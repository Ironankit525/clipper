"""YouTube URL -> N captioned vertical clips, ranked by predicted performance."""
import hashlib
import json
import os
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import captions, config, download, reframe, render, select, transcribe

STATE_FILE = "job.json"
TERMINAL = ("done", "error", "interrupted")
SAVE_EVERY = 2.0          # seconds between state writes while a stage is chatty
MAX_LOGS = 800            # keep job.json small on hours-long runs


@dataclass
class Job:
    id: str
    url: str
    n_clips: int = 6
    min_s: int = 20
    max_s: int = 60
    status: str = "queued"
    title: str = ""
    logs: list[str] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)
    error: str | None = None
    progress: dict = field(default_factory=lambda: {"percent": 0.0, "stage": "queued", "text": "In queue"})
    resumes: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    _last_save: float = 0.0

    @property
    def workdir(self) -> Path:
        return config.DATA / self.id

    def log(self, message: str) -> None:
        self.logs.append(message)
        if len(self.logs) > MAX_LOGS:
            del self.logs[:-MAX_LOGS]
        # A dead controlling terminal makes print() raise EIO, which used to kill
        # the whole run mid-download. Logging must never take the pipeline down.
        try:
            print(f"[{self.id}] {message}", flush=True)
        except OSError:
            pass
        self.save()

    def set_progress(self, percent: float, text: str = "", stage: str | None = None) -> None:
        self.progress = {
            "percent": round(max(0.0, min(100.0, float(percent))), 1),
            "stage": stage or self.status,
            "text": text,
        }
        self.save()

    def set_status(self, status: str) -> None:
        self.status = status
        self.save(force=True)

    def save(self, force: bool = False) -> None:
        """Mirror the job to disk so it survives restarts and server reloads."""
        now = time.time()
        if not force and self.status not in TERMINAL and now - self._last_save < SAVE_EVERY:
            return
        self._last_save = now
        self.updated = now
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
            tmp = self.workdir / (STATE_FILE + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.workdir / STATE_FILE)
        except OSError:
            pass

    def summary(self) -> dict:
        return {"id": self.id, "url": self.url, "title": self.title, "status": self.status,
                "n_clips": self.n_clips, "clips": len(self.clips), "error": self.error,
                "created": self.created, "updated": self.updated,
                "thumb": self.clips[0]["thumb"] if self.clips else None}


def load(jid: str) -> Job | None:
    path = config.DATA / jid / STATE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fields = {f for f in Job.__dataclass_fields__ if not f.startswith("_")}
    job = Job(**{k: v for k, v in data.items() if k in fields})
    # Nothing is running after a restart, so an in-flight job is really stranded.
    if job.status not in TERMINAL:
        job.status = "interrupted"
        job.progress = {"percent": job.progress.get("percent", 0.0), "stage": "interrupted",
                        "text": "Stopped when the server restarted - run it again to resume."}
    return job


def load_all() -> dict[str, "Job"]:
    jobs = {}
    for d in sorted(config.DATA.glob("*/" + STATE_FILE)):
        job = load(d.parent.name)
        if job:
            jobs[job.id] = job
    return jobs


def run(job: Job) -> None:
    workdir = job.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    job.error = None
    try:
        job.set_status("downloading")
        job.set_progress(0.0, "Connecting to YouTube...", stage="downloading")
        video, audio, meta = download.download(
            job.url, workdir, job.log,
            on_progress=lambda pct, txt: job.set_progress(pct, txt, stage="downloading")
        )
        job.title = meta.get("title") or job.title

        job.set_status("transcribing")
        job.set_progress(0.0, "Starting transcription...", stage="transcribing")
        transcript = transcribe.transcribe(
            audio, workdir, job.log,
            on_progress=lambda pct, txt: job.set_progress(pct, txt, stage="transcribing")
        )

        job.set_status("analysing")
        job.set_progress(50.0, f"Selecting top moments with {config.GEMINI_SELECT_MODEL}...", stage="analysing")
        picks = select.select(transcript, meta["duration"], job.n_clips,
                              job.min_s, job.max_s, job.log)
        if not picks:
            raise RuntimeError("No usable clips came back from the model.")

        job.set_status("rendering")
        job.clips = []
        total_picks = len(picks)
        for idx, pick in enumerate(picks, 1):
            render_pct = int(((idx - 1) / total_picks) * 100)
            job.set_progress(render_pct, f"Rendering clip {idx}/{total_picks} ({pick['virality']} score)...", stage="rendering")
            job.log(f"Rendering clip {idx}/{len(picks)} "
                    f"({pick['end'] - pick['start']:.0f}s, score {pick['virality']})")

            words = [
                {"word": w["word"],
                 "start": w["start"] - pick["start"],
                 "end": w["end"] - pick["start"]}
                for w in transcript["words"]
                if w["end"] > pick["start"] and w["start"] < pick["end"]
            ]

            ass = captions.build(
                words, pick["hook"], pick["end"] - pick["start"],
                workdir / f"clip_{idx:02d}.ass",
            )
            crop = reframe.plan(video, pick["start"], pick["end"],
                                meta["width"], meta["height"], job.log)
            out = workdir / f"clip_{idx:02d}.mp4"
            render.render(video, pick["start"], pick["end"], ass, crop,
                          out, workdir, job.log)
            thumb = render.thumbnail(out, workdir / f"clip_{idx:02d}.jpg")

            job.clips.append({
                **pick,
                "index": idx,
                "duration": round(pick["end"] - pick["start"], 1),
                "file": out.name,
                "thumb": thumb.name,
            })

        (workdir / "clips.json").write_text(json.dumps(job.clips, indent=2))
        from . import storage, uploads   # local import: both import pipeline
        uploads.sync()
        swept = storage.clean(job.id, source=config.CLEAN_SOURCE_AFTER_RENDER)
        if swept["freed"]:
            job.log(f"Freed {storage.human(swept['freed'])} of working files.")
        job.resumes = 0                  # finished cleanly; start the count over
        job.set_progress(100.0, f"Done! {len(job.clips)} clips ready.", stage="done")
        job.set_status("done")
        job.log(f"Finished - {len(job.clips)} clips ready.")

    except Exception as exc:                      # surface the real cause in the UI
        job.error = str(exc)
        job.set_status("error")
        job.set_progress(0.0, f"Error: {exc}", stage="error")
        job.log(f"ERROR: {exc}")
        try:
            traceback.print_exc()
        except OSError:
            pass


def job_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]

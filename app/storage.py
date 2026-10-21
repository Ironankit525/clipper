"""Reclaim disk space without losing anything that matters.

A finished job is mostly dead weight: a 2h47m source is ~3.6GB, its extracted
audio another 38MB, and once a clip is on YouTube the local mp4 is a copy of
something already published. What is worth keeping is small - the transcript
(so clips can be re-selected without paying for transcription again), the
thumbnails the dashboard shows, and the job metadata.

Four kinds of file, deleted under different conditions:

    source      source.mp4     once clips are rendered  (re-downloadable)
    working     audio.mp3,     once clips are rendered  (regenerable)
                *.ass, *.part
    posted      clip_NN.mp4    once that clip is on YouTube
    keep        transcript.json, clips.json, job.json, *.jpg, title.txt
"""
import json
from pathlib import Path

from . import config

SOURCE_NAMES = ("source.mp4",)
WORKING_GLOBS = ("audio.mp3", "*.ass", "*.part", "*.ytdl", "source.f*.mp4",
                 "source.f*.m4a", "*.json.tmp")
KEEP_NAMES = ("transcript.json", "clips.json", "job.json", "title.txt")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink(path: Path) -> int:
    freed = _size(path)
    try:
        path.unlink()
        return freed
    except OSError:
        return 0


def _job_dirs() -> list[Path]:
    return sorted(d for d in config.DATA.glob("*") if d.is_dir())


def _posted_files(job_dir: Path) -> set[str]:
    """Clip filenames already published, per the upload registry."""
    from . import uploads                       # local: uploads imports pipeline
    return {row["file"] for row in uploads.all_rows()
            if row["job_id"] == job_dir.name and row["status"] == uploads.UPLOADED}


def _rendered(job_dir: Path) -> bool:
    try:
        return bool(json.loads((job_dir / "clips.json").read_text()))
    except (OSError, ValueError):
        return False


def usage(job_id: str) -> dict:
    """What this job occupies, split by what is safe to delete."""
    job_dir = config.DATA / job_id
    if not job_dir.is_dir():
        return {"total": 0, "source": 0, "working": 0, "posted": 0, "keep": 0,
                "reclaimable": 0, "rendered": False}

    posted = _posted_files(job_dir)
    rendered = _rendered(job_dir)
    buckets = {"source": 0, "working": 0, "posted": 0, "keep": 0, "other": 0}

    working = {p.name for glob in WORKING_GLOBS for p in job_dir.glob(glob)}
    for path in job_dir.iterdir():
        if not path.is_file():
            continue
        size = _size(path)
        if path.name in SOURCE_NAMES:
            buckets["source"] += size
        elif path.name in working:
            buckets["working"] += size
        elif path.name in posted:
            buckets["posted"] += size
        elif path.name in KEEP_NAMES or path.suffix in (".jpg", ".mp4"):
            buckets["keep"] += size
        else:
            buckets["other"] += size

    reclaimable = buckets["working"] + buckets["posted"] + (
        buckets["source"] if rendered else 0)
    return {
        "job_id": job_id,
        "total": sum(buckets.values()),
        **buckets,
        "reclaimable": reclaimable,
        "rendered": rendered,
    }


def clean(job_id: str, source: bool = False, working: bool = True,
          posted: bool = False) -> dict:
    """Delete the chosen categories. Never touches a clip still to be posted."""
    job_dir = config.DATA / job_id
    if not job_dir.is_dir():
        raise KeyError("no such job")

    freed, removed = 0, []

    if working:
        for glob in WORKING_GLOBS:
            for path in job_dir.glob(glob):
                gone = _unlink(path)
                if gone:
                    freed += gone
                    removed.append(path.name)

    if source and _rendered(job_dir):
        for name in SOURCE_NAMES:
            gone = _unlink(job_dir / name)
            if gone:
                freed += gone
                removed.append(name)

    if posted:
        for name in _posted_files(job_dir):
            gone = _unlink(job_dir / name)
            if gone:
                freed += gone
                removed.append(name)

    return {"job_id": job_id, "freed": freed, "removed": sorted(removed)}


def sweep(source: bool = True, posted: bool = True) -> dict:
    """Clean every job at once. Only finished jobs give up their source."""
    freed, touched = 0, []
    for job_dir in _job_dirs():
        result = clean(job_dir.name, source=source, working=True, posted=posted)
        if result["freed"]:
            freed += result["freed"]
            touched.append(result)
    return {"freed": freed, "jobs": touched}


def overview() -> dict:
    jobs = [usage(d.name) for d in _job_dirs()]
    return {
        "jobs": {j["job_id"]: j for j in jobs},
        "total": sum(j["total"] for j in jobs),
        "reclaimable": sum(j["reclaimable"] for j in jobs),
    }


def human(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} GB"

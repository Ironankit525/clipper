"""Fetch a YouTube video with yt-dlp, plus a compact audio track for transcription."""
import subprocess
from pathlib import Path

from . import config


def title_of(url: str, workdir: Path) -> str:
    """Cheap metadata-only fetch, cached so re-runs stay offline-friendly."""
    cache = workdir / "title.txt"
    if cache.exists():
        return cache.read_text().strip()
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-playlist", "--skip-download", "--print", "%(title)s", url],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip().splitlines()
        title = out[0] if out else ""
    except (OSError, subprocess.SubprocessError):
        title = ""
    if title:
        cache.write_text(title)
    return title


def download(url: str, workdir: Path, log, on_progress=None) -> tuple[Path, Path, dict]:
    workdir.mkdir(parents=True, exist_ok=True)
    video = workdir / "source.mp4"
    title = title_of(url, workdir)
    if title:
        log(f"Video: {title}")

    if video.exists() and not playable(video):
        log("Existing source.mp4 is incomplete - downloading again.")
        video.unlink()

    if not video.exists():
        log("Downloading video...")
        if on_progress:
            on_progress(0, "Connecting to YouTube...")

        cmd = [
            "yt-dlp",
            "--newline",
            "--progress-template", "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._downloaded_bytes_str)s|%(progress._total_bytes_str,progress._total_bytes_estimate_str)s",
            "-f", (f"bv*[height<={config.MAX_HEIGHT}][vcodec^=avc1]+ba[ext=m4a]/"
                   f"bv*[height<={config.MAX_HEIGHT}]+ba/b[height<={config.MAX_HEIGHT}]/b"),
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(video),
            url,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        import time
        last_log_pct = -10.0
        last_log_time = 0.0

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("download:"):
                parts = line[len("download:"):].split("|")
                raw_pct = parts[0].strip().replace("%", "") if len(parts) > 0 else "0"
                speed = parts[1].strip() if len(parts) > 1 else ""
                eta = parts[2].strip() if len(parts) > 2 else ""
                downloaded = parts[3].strip() if len(parts) > 3 else ""
                total = parts[4].strip() if len(parts) > 4 else ""

                try:
                    pct = float(raw_pct)
                except ValueError:
                    pct = 0.0

                extra = []
                if downloaded and total and total != "NA":
                    extra.append(f"{downloaded}/{total}")
                elif downloaded:
                    extra.append(downloaded)
                if speed and speed != "Unknown B/s":
                    extra.append(speed)
                if eta and eta not in ("Unknown", "NA"):
                    extra.append(f"ETA {eta}")

                status_str = f"Downloading: {pct:.1f}%"
                if extra:
                    status_str += f" ({', '.join(extra)})"

                if on_progress:
                    on_progress(pct, status_str)

                now = time.time()
                if pct >= last_log_pct + 10 or (now - last_log_time > 8 and pct > last_log_pct):
                    last_log_pct = pct
                    last_log_time = now
                    log(status_str)
            elif "Destination:" in line:
                log(line)
            elif "Merging" in line:
                if on_progress:
                    on_progress(99.0, "Merging video and audio streams...")
                log("Merging streams into mp4...")

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        if on_progress:
            on_progress(100.0, "Video downloaded.")

    # 16k mono mp3 keeps us under the API's 25MB limit for long videos
    audio = workdir / "audio.mp3"
    if not audio.exists():
        log("Extracting audio...")
        subprocess.run(
            [config.FFMPEG, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "32k", str(audio)],
            check=True, capture_output=True, text=True,
        )

    meta = probe(video)
    meta["title"] = title
    log(f"Source: {meta['width']}x{meta['height']}, {meta['duration']:.0f}s")
    return video, audio, meta


def playable(path: Path) -> bool:
    """True only if ffprobe can read a video stream out of the file."""
    try:
        probe(path)
        return True
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        return False


def probe(path: Path) -> dict:
    import json
    out = subprocess.run(
        [config.FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "duration": float(info["format"]["duration"]),
        "fps": eval_fps(v.get("avg_frame_rate", "30/1")),
    }


def eval_fps(frac: str) -> float:
    try:
        num, den = frac.split("/")
        return float(num) / float(den) if float(den) else 30.0
    except Exception:
        return 30.0

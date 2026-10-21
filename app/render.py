"""Cut, reframe, caption and encode one clip with a single ffmpeg pass."""
import subprocess
from pathlib import Path

from . import reframe
from .config import FFMPEG, OUT_H, OUT_W


def render(video: Path, start: float, end: float, ass: Path,
           crop: dict, out: Path, workdir: Path, log) -> Path:
    duration = end - start
    chain = []

    if crop["mode"] == "track":
        # crop re-evaluates x every frame, so the move is per-frame smooth
        # rather than the 2Hz staircase that sendcmd keyframes produced.
        expr = reframe.crop_x_expr(crop["keys"])
        chain.append(f"crop=w={crop['crop_w']}:h={crop['crop_h']}:x='{expr}':y=0")
    elif crop["mode"] == "static":
        chain.append(f"crop=w={crop['crop_w']}:h={crop['crop_h']}:x={crop['x']}:y=0")

    chain.append(f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase")
    chain.append(f"crop={OUT_W}:{OUT_H}")
    chain.append("setsar=1")
    chain.append(f"subtitles=f='{_esc(ass)}'")

    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
        "-vf", ",".join(chain),
        "-r", "30",
        "-c:v", "h264_videotoolbox", "-b:v", "8M", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # VideoToolbox can refuse odd inputs; libx264 always works
        log("  hardware encoder declined, falling back to libx264")
        cmd[cmd.index("h264_videotoolbox")] = "libx264"
        cmd[cmd.index("-b:v")] = "-crf"
        cmd[cmd.index("8M")] = "20"
        cmd.insert(cmd.index("-crf"), "-preset")
        cmd.insert(cmd.index("-preset") + 1, "veryfast")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-1500:]}")

    return out


def thumbnail(clip: Path, out: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-ss", "0.5", "-i", str(clip), "-frames:v", "1",
         "-vf", "scale=270:-1", str(out)],
        capture_output=True, text=True,
    )
    return out


def _esc(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")

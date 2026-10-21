import os
import shutil
import subprocess
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

SELECT_PROVIDER = os.getenv("SELECT_PROVIDER", "gemini").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_SELECT_MODEL = os.getenv("OPENAI_SELECT_MODEL", "gpt-4o").strip()
GEMINI_SELECT_MODEL = os.getenv("GEMINI_SELECT_MODEL", "gemini-3.8-flash").strip()

TRANSCRIBE_PROVIDER = os.getenv("TRANSCRIBE_PROVIDER", "openai").strip().lower()
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small.en").strip()

# Vertical short format
OUT_W, OUT_H = 1080, 1920

# Source ceiling. The 9:16 crop takes ~56% of the frame width, so 1080p in
# still gives a sharp 1080x1920 out; drop to 720 to download ~3x less.
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "1080"))

# --- YouTube Shorts upload, driven by the local n8n workflow ---
# Clipper never talks to YouTube itself; it only keeps the queue and tells n8n
# what to post next. PUBLIC_URL is how n8n reaches this server.
YT_PRIVACY = os.getenv("YT_PRIVACY", "public").strip().lower()
YT_DAILY_LIMIT = int(os.getenv("YT_DAILY_LIMIT", "10"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/clipper-upload").strip()

# A job interrupted by a restart picks up where it left off (download and
# transcript are cached), so the default is to just carry on.
AUTO_RESUME = os.getenv("AUTO_RESUME", "true").lower() == "true"
MAX_AUTO_RESUMES = int(os.getenv("MAX_AUTO_RESUMES", "2"))

# --- Housekeeping ---
# Working files (extracted audio, caption files, download leftovers) are always
# removed once a job renders: they cost GBs and regenerate for free. The source
# video and already-posted clips are bigger decisions, so they have switches.
# Deleting the source means a re-render has to download the video again.
CLEAN_SOURCE_AFTER_RENDER = os.getenv("CLEAN_SOURCE_AFTER_RENDER", "false").lower() == "true"
CLEAN_CLIP_AFTER_UPLOAD = os.getenv("CLEAN_CLIP_AFTER_UPLOAD", "true").lower() == "true"


# Homebrew ships a slim `ffmpeg` without libass and a keg-only `ffmpeg-full`
# with it. Captions are burned in via the subtitles filter, so prefer whichever
# build actually has it rather than whatever landed on PATH first.
_CANDIDATES = [
    os.getenv("FFMPEG_BIN", "").strip() or None,
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    shutil.which("ffmpeg"),
]


def ffmpeg_has(filter_name: str, binary: str | None = None) -> bool:
    try:
        out = subprocess.run([binary or FFMPEG, "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [filter_name]
               for line in out.splitlines() if line.strip())


def _pick_ffmpeg() -> str:
    present = [c for c in _CANDIDATES if c and Path(c).exists()]
    for candidate in present:
        if ffmpeg_has("subtitles", candidate):
            return candidate
    return present[0] if present else "ffmpeg"


FFMPEG = _pick_ffmpeg()
FFPROBE = str(Path(FFMPEG).with_name("ffprobe")) if Path(FFMPEG).parent.name == "bin" \
    else "ffprobe"


def check_ready() -> list[str]:
    """Return human-readable config problems; empty list means good to go."""
    problems = []
    if SELECT_PROVIDER == "openai" and not OPENAI_API_KEY:
        problems.append("SELECT_PROVIDER=openai but OPENAI_API_KEY is empty")
    if SELECT_PROVIDER == "gemini" and not GEMINI_API_KEY:
        problems.append("SELECT_PROVIDER=gemini but GEMINI_API_KEY is empty")
    if SELECT_PROVIDER not in ("openai", "gemini"):
        problems.append(f"SELECT_PROVIDER must be openai or gemini, got {SELECT_PROVIDER!r}")
    if TRANSCRIBE_PROVIDER == "gemini" and not GEMINI_API_KEY:
        problems.append("TRANSCRIBE_PROVIDER=gemini but GEMINI_API_KEY is empty")
    if TRANSCRIBE_PROVIDER not in ("openai", "gemini", "local"):
        problems.append(f"TRANSCRIBE_PROVIDER must be openai, gemini or local, got {TRANSCRIBE_PROVIDER!r}")
    if TRANSCRIBE_PROVIDER == "openai" and not OPENAI_API_KEY:
        problems.append("TRANSCRIBE_PROVIDER=openai but OPENAI_API_KEY is empty")
    if not Path(FFMPEG).exists() and not shutil.which(FFMPEG):
        problems.append("ffmpeg not found - brew install ffmpeg-full")
    elif not ffmpeg_has("subtitles"):
        problems.append(
            "this ffmpeg has no 'subtitles' filter, so captions cannot be burned in "
            "- brew install ffmpeg-full"
        )
    if not shutil.which("yt-dlp"):
        problems.append("yt-dlp is not on PATH - brew install yt-dlp")
    return problems

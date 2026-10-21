"""Transcription with WORD-level timestamps.

Word timing is the backbone of this whole tool: it decides where cuts can land
without chopping a syllable, and it drives the karaoke captions.
"""
import base64
import json
import subprocess
import time
from pathlib import Path

import httpx

from . import config

# OpenAI caps uploads at 25MB; 10-minute chunks of 32kbps mono stay well under.
CHUNK_SECONDS = 600


def transcribe(audio: Path, workdir: Path, log, on_progress=None) -> dict:
    cache = workdir / "transcript.json"
    if cache.exists():
        log("Transcript cached, reusing.")
        if on_progress:
            on_progress(100.0, "Transcript cached, reusing.")
        return json.loads(cache.read_text())

    if config.TRANSCRIBE_PROVIDER == "local":
        result = _local(audio, log, on_progress=on_progress)
    elif config.TRANSCRIBE_PROVIDER == "gemini":
        if on_progress:
            on_progress(10.0, "Transcribing with Gemini...")
        result = _gemini(audio, log)
    else:
        if on_progress:
            on_progress(10.0, "Transcribing with OpenAI...")
        result = _openai(audio, workdir, log)

    cache.write_text(json.dumps(result))
    log(f"Transcribed {len(result['words'])} words.")
    return result


# --------------------------------------------------------------------------- #
# OpenAI whisper-1 - the only widely available API that returns word offsets
# --------------------------------------------------------------------------- #
def _openai(audio: Path, workdir: Path, log) -> dict:
    duration = _duration(audio)
    chunks = _split(audio, workdir, duration, log)
    words, segments = [], []

    for idx, (chunk, offset) in enumerate(chunks, 1):
        log(f"Transcribing chunk {idx}/{len(chunks)}...")
        with httpx.Client(timeout=900) as client:
            resp = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                files={"file": (chunk.name, chunk.read_bytes(), "audio/mpeg")},
                data={
                    "model": "whisper-1",
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": ["word", "segment"],
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI transcription failed: {resp.status_code} {resp.text[:400]}")
        body = resp.json()

        for w in body.get("words") or []:
            words.append({
                "word": w["word"],
                "start": float(w["start"]) + offset,
                "end": float(w["end"]) + offset,
            })
        for s in body.get("segments") or []:
            segments.append({
                "text": s["text"].strip(),
                "start": float(s["start"]) + offset,
                "end": float(s["end"]) + offset,
            })

    return {"words": words, "segments": segments}


def _split(audio: Path, workdir: Path, duration: float, log) -> list[tuple[Path, float]]:
    if duration <= CHUNK_SECONDS and audio.stat().st_size < 24_000_000:
        return [(audio, 0.0)]

    out = []
    count = int(duration // CHUNK_SECONDS) + 1
    log(f"Splitting audio into {count} chunks for upload...")
    for i in range(count):
        offset = i * CHUNK_SECONDS
        part = workdir / f"chunk_{i:03d}.mp3"
        if not part.exists():
            subprocess.run(
                [config.FFMPEG, "-y", "-ss", str(offset), "-t", str(CHUNK_SECONDS),
                 "-i", str(audio), "-c", "copy", str(part)],
                check=True, capture_output=True, text=True,
            )
        if part.stat().st_size > 1000:
            out.append((part, float(offset)))
    return out


# --------------------------------------------------------------------------- #
# Local fallback - faster-whisper. Free, no API key, runs fine on Apple silicon.
# --------------------------------------------------------------------------- #
def _local(audio: Path, log, on_progress=None) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "TRANSCRIBE_PROVIDER=local needs faster-whisper: uv pip install faster-whisper"
        ) from exc

    log(f"Loading local whisper ({config.LOCAL_WHISPER_MODEL})...")
    if on_progress:
        on_progress(5.0, f"Loading whisper model ({config.LOCAL_WHISPER_MODEL})...")
    model = WhisperModel(config.LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8")
    
    if on_progress:
        on_progress(10.0, "Decoding audio stream...")
    # Decode with ffmpeg rather than letting faster-whisper reach for PyAV, whose
    # current release dropped a kwarg it still passes.
    pcm = _pcm(audio)
    total_sec = len(pcm) / 16000.0 if len(pcm) else 1.0

    if on_progress:
        on_progress(12.0, f"Transcribing audio ({total_sec:.0f}s)...")

    seg_iter, _ = model.transcribe(pcm, word_timestamps=True, vad_filter=True)

    words, segments = [], []
    import time
    last_log_time = 0.0

    for seg in seg_iter:
        segments.append({"text": seg.text.strip(), "start": seg.start, "end": seg.end})
        for w in seg.words or []:
            words.append({"word": w.word, "start": w.start, "end": w.end})
        if total_sec > 0:
            pct = min(99.0, 12.0 + (seg.end / total_sec) * 87.0)
            txt = f"Transcribing: {pct:.1f}% ({seg.end:.0f}s / {total_sec:.0f}s)"
            if on_progress:
                on_progress(pct, txt)
            now = time.time()
            if now - last_log_time > 15:
                last_log_time = now
                log(txt)

    if on_progress:
        on_progress(100.0, f"Transcribed {len(words)} words.")
    return {"words": words, "segments": segments}


def _pcm(audio: Path):
    """Decode to the mono float32 16kHz array whisper wants."""
    import numpy as np

    raw = subprocess.run(
        [config.FFMPEG, "-v", "quiet", "-i", str(audio),
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _duration(path: Path) -> float:
    out = subprocess.run(
        [config.FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


# --------------------------------------------------------------------------- #
# Gemini - no local model at all, but it only gives segment-level timing, so
# word times are interpolated inside each segment. Good enough to cut on,
# slightly loose for karaoke captions.
# --------------------------------------------------------------------------- #
GEMINI_PROMPT = """Transcribe this audio completely and accurately.

Return STRICT JSON only, no markdown fence:
{"segments": [{"start": <seconds as a number>, "end": <seconds as a number>, "text": "<verbatim speech>"}]}

Rules:
- Break at natural sentence or clause boundaries, 3 to 12 seconds per segment.
- Timestamps in SECONDS from the start of the audio, as plain numbers.
- Cover the entire audio with no gaps and no overlaps.
- Transcribe verbatim. Do not summarise, translate, or clean up grammar.
- Omit non-speech noise entirely."""


def _gemini(audio: Path, log) -> dict:
    size_mb = audio.stat().st_size / 1_000_000
    log(f"Transcribing {size_mb:.1f}MB with Gemini...")

    part = (_gemini_upload(audio, log) if size_mb > 18
            else {"inlineData": {"mimeType": "audio/mpeg",
                                 "data": base64.b64encode(audio.read_bytes()).decode()}})

    model = config.GEMINI_SELECT_MODEL
    if model in ("gemini-2.5-flash", "gemini-1.5-flash"):
        model = "gemini-3.8-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    with httpx.Client(timeout=1800) as client:
        resp = client.post(
            url,
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json={
                "contents": [{"role": "user",
                              "parts": [{"text": GEMINI_PROMPT}, part]}],
                "generationConfig": {"responseMimeType": "application/json",
                                     "temperature": 0.0},
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini transcription failed: {resp.status_code} {resp.text[:400]}")

    body = resp.json()
    try:
        raw = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(body)[:400]}") from exc

    segments = []
    for s in json.loads(raw).get("segments", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            segments.append({"text": text, "start": start, "end": end})

    segments.sort(key=lambda s: s["start"])
    return {"segments": segments, "words": _interpolate(segments)}


def _gemini_upload(audio: Path, log) -> dict:
    """Push the file through the Files API when it is too big to inline."""
    log("  file is large, uploading via Files API...")
    size = audio.stat().st_size
    with httpx.Client(timeout=1800) as client:
        start = client.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            headers={
                "x-goog-api-key": config.GEMINI_API_KEY,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "audio/mpeg",
            },
            json={"file": {"display_name": audio.name}},
        )
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError(f"Files API did not return an upload URL: {start.text[:300]}")

        done = client.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            content=audio.read_bytes(),
        )
        info = done.json()["file"]

        # the file has to leave PROCESSING before it can be referenced
        for _ in range(60):
            if info.get("state") == "ACTIVE":
                break
            time.sleep(2)
            info = client.get(
                f"https://generativelanguage.googleapis.com/v1beta/{info['name']}",
                headers={"x-goog-api-key": config.GEMINI_API_KEY},
            ).json()

    return {"fileData": {"mimeType": "audio/mpeg", "fileUri": info["uri"]}}


def _interpolate(segments: list[dict]) -> list[dict]:
    """Spread each segment's words across its span, weighted by word length."""
    words = []
    for seg in segments:
        tokens = seg["text"].split()
        if not tokens:
            continue
        span = max(0.2, seg["end"] - seg["start"])
        weights = [len(t) + 1 for t in tokens]
        total = sum(weights)
        cursor = seg["start"]
        for token, weight in zip(tokens, weights):
            length = span * weight / total
            words.append({"word": token, "start": round(cursor, 3),
                          "end": round(cursor + length, 3)})
            cursor += length
    return words

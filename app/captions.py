"""Build an ASS subtitle file with word-by-word highlighting.

The look: 2-3 big words at a time, centred low in the frame, the word currently
being spoken popped slightly larger and in colour. One Dialogue event per word
gives frame-accurate timing without any animation framework.
"""
from pathlib import Path

from .config import OUT_H, OUT_W

MAX_WORDS_PER_LINE = 3
MAX_CHARS_PER_LINE = 22

# ASS colours are &HAABBGGRR (alpha, blue, green, red)
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
HIGHLIGHT = "&H0000E5FF"   # amber
HOOK_BG = "&H40000000"

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,96,{WHITE},{WHITE},{BLACK},{BLACK},-1,0,0,0,100,100,0,0,1,9,4,2,80,80,480,1
Style: Hook,Arial Black,78,{WHITE},{WHITE},{BLACK},{HOOK_BG},-1,0,0,0,100,100,0,0,3,6,0,8,90,90,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build(words: list[dict], hook: str, clip_duration: float, out: Path) -> Path:
    """words: clip-relative word timings. Writes an .ass file and returns its path."""
    events = []

    if hook:
        events.append(_event("Hook", 0.0, min(2.6, clip_duration), _wrap_hook(hook)))

    for line in _group(words):
        for idx, word in enumerate(line):
            start = word["start"]
            # hold the last word of a line until the next word starts
            end = line[idx + 1]["start"] if idx + 1 < len(line) else max(word["end"], start + 0.12)
            events.append(_event("Caption", start, end, _render(line, idx)))

    out.write_text(HEADER + "\n".join(events) + "\n", encoding="utf-8")
    return out


def _group(words: list[dict]) -> list[list[dict]]:
    """Chunk words into short lines, breaking on long pauses and sentence ends."""
    lines, current, chars = [], [], 0
    for word in words:
        text = word["word"].strip()
        if not text:
            continue
        gap = current and (word["start"] - current[-1]["end"] > 0.6)
        too_long = len(current) >= MAX_WORDS_PER_LINE or chars + len(text) > MAX_CHARS_PER_LINE
        if current and (gap or too_long):
            lines.append(current)
            current, chars = [], 0
        current.append({"word": text, "start": word["start"], "end": word["end"]})
        chars += len(text) + 1
        if text.endswith((".", "!", "?")):
            lines.append(current)
            current, chars = [], 0
    if current:
        lines.append(current)
    return lines


def _render(line: list[dict], active: int) -> str:
    parts = []
    for i, word in enumerate(line):
        text = _escape(word["word"].upper())
        if i == active:
            parts.append(f"{{\\c{HIGHLIGHT}&\\fscx108\\fscy108}}{text}{{\\r}}")
        else:
            parts.append(text)
    return " ".join(parts)


def _wrap_hook(hook: str) -> str:
    words, lines, current = hook.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 20 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\\N".join(_escape(line.upper()) for line in lines[:3])


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _event(style: str, start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}"


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"

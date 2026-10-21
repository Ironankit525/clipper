"""Pick the moments most likely to perform as shorts.

Sends the timestamped transcript to Gemini or OpenAI and asks for self-contained
clips with a hook, then snaps the returned times onto real word boundaries so no
clip starts or ends mid-syllable.
"""
import json
import random
import re
import time

import httpx

RETRY_STATUS = (408, 429, 500, 502, 503, 504)
RETRIES = 4

from . import config

SYSTEM = """You are a short-form video editor who has shipped thousands of clips \
that performed on TikTok, Reels and YouTube Shorts. You are given a timestamped \
transcript of a long-form video. Your job is to find the moments that would \
perform best as standalone vertical shorts.

What makes a clip work:
- It is SELF-CONTAINED. A viewer with zero context understands it completely.
- The first 3 seconds are a hook: a bold claim, a question, a number, a confession, \
conflict, or a pattern interrupt. Never start on throat-clearing like "so", "yeah", \
or "I think basically".
- In an interview or podcast, the strongest structure is QUESTION THEN ANSWER. If the \
moment is an answer, START ON THE QUESTION THAT PROMPTED IT, from its first word. The \
question is what creates the tension the answer pays off, and an answer alone drops \
the viewer into the middle of a conversation. Include the whole question, never its \
tail. Skip the question only when it is rambling, boring, or the answer genuinely \
stands alone as a statement.
- There is tension and then a payoff inside the clip. A setup with no resolution dies.
- It has a reason to be re-watched or argued about: a strong opinion, a \
counter-intuitive fact, a story with a turn, a specific actionable tip, or emotion.
- It ends on the punchline or resolution, not on trailing filler.

Hard rules:
- Clips must be between {min_s} and {max_s} seconds long.
- Clips must NOT overlap each other.
- Prefer spread across the whole video over clustering in one section.
- Only use timestamps that exist in the transcript. Never invent content.
- Rank by genuine potential, not by order of appearance.

Return STRICT JSON only, no markdown fence, in this exact shape:
{{"clips": [{{
  "start": <seconds, number>,
  "end": <seconds, number>,
  "hook": "<on-screen title, max 60 chars, punchy, no hashtags>",
  "title": "<suggested post caption, max 100 chars>",
  "virality": <integer 0-100>,
  "reason": "<one sentence on why this will perform>",
  "question": "<the setup question if the clip opens on one, else empty string>"
}}]}}"""

USER = """Find the {n} strongest short-form clips in this transcript.

Video duration: {duration:.0f} seconds.

QUESTIONS ASKED IN THIS VIDEO. Each is a candidate opening: a clip that opens \
on the question and runs through the answer beats the answer alone, because the \
question is the tension. When the answer that follows one of these is strong, \
set the clip start to that question's timestamp.
{questions}

TRANSCRIPT (each line is `[start -> end] text`):
{transcript}"""

MAX_QUESTION_CANDIDATES = 90


def select(transcript: dict, duration: float, n_clips: int,
           min_s: int, max_s: int, log) -> list[dict]:
    lines = [
        f"[{s['start']:.1f} -> {s['end']:.1f}] {s['text']}"
        for s in transcript["segments"] if s["text"]
    ]
    if not lines:
        raise RuntimeError("Transcript had no speech segments - is there dialogue in this video?")

    asked = [(seg["start"], question_text(seg["text"]))
             for seg in transcript["segments"]]
    asked = [(t, q) for t, q in asked if q][:MAX_QUESTION_CANDIDATES]
    questions = "\n".join(f"[{t:.1f}] {q}" for t, q in asked) or "(none found)"

    system = SYSTEM.format(min_s=min_s, max_s=max_s)
    user = USER.format(n=n_clips, duration=duration, questions=questions,
                       transcript="\n".join(lines))
    log(f"  {len(asked)} question(s) offered as clip openings")

    log(f"Analysing {len(lines)} transcript segments with {config.SELECT_PROVIDER}...")
    raw = _gemini(system, user) if config.SELECT_PROVIDER == "gemini" else _openai(system, user)

    clips = _parse(raw)
    clips = [c for c in clips if c["end"] > c["start"]]
    clips = _open_on_question(clips, transcript["segments"], max_s, log)
    clips = _snap(clips, transcript["words"], duration, min_s, max_s)
    clips = _dedupe(clips)
    clips.sort(key=lambda c: -c["virality"])
    clips = clips[:n_clips]

    log(f"Selected {len(clips)} clips (scores: {', '.join(str(c['virality']) for c in clips)}).")
    return clips


def _post(send) -> httpx.Response:
    """Selection is one call at the end of a long pipeline; a provider hiccup
    should cost a few seconds, not the whole run."""
    last = None
    for attempt in range(RETRIES):
        try:
            resp = send()
        except httpx.HTTPError as exc:
            last = exc
        else:
            if resp.status_code not in RETRY_STATUS:
                return resp
            last = resp
        if attempt < RETRIES - 1:
            time.sleep(min(2 ** attempt * 3, 30) + random.uniform(0, 1.5))
    if isinstance(last, httpx.Response):
        return last
    raise RuntimeError(f"selection request failed: {last}")


def _openai(system: str, user: str) -> str:
    with httpx.Client(timeout=600) as client:
        resp = _post(lambda: client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            json={
                "model": config.OPENAI_SELECT_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
            },
        ))
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI selection failed: {resp.status_code} {resp.text[:400]}")
    return resp.json()["choices"][0]["message"]["content"]


def _gemini(system: str, user: str) -> str:
    model = config.GEMINI_SELECT_MODEL
    if model in ("gemini-2.5-flash", "gemini-1.5-flash"):
        model = "gemini-3.8-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    with httpx.Client(timeout=600) as client:
        resp = _post(lambda: client.post(
            url,
            headers={"x-goog-api-key": config.GEMINI_API_KEY},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.7,
                },
            },
        ))
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini selection failed: {resp.status_code} {resp.text[:400]}")
    body = resp.json()
    try:
        return body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(body)[:400]}") from exc


def _parse(raw: str) -> list[dict]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise RuntimeError(f"Model did not return JSON: {text[:300]}")
        data = json.loads(match.group(0))

    clips = data.get("clips", data if isinstance(data, list) else [])
    out = []
    for c in clips:
        try:
            out.append({
                "start": float(c["start"]),
                "end": float(c["end"]),
                "hook": str(c.get("hook", "")).strip()[:60],
                "title": str(c.get("title", "")).strip()[:120],
                "virality": max(0, min(100, int(float(c.get("virality", 50))))),
                "reason": str(c.get("reason", "")).strip(),
                "question": str(c.get("question", "")).strip()[:300],
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


QUESTION_LOOKBACK = 16.0   # seconds of setup worth pulling in front of a payoff
SENTENCE_END = (".", "?", "!")

# Most question marks in a podcast are the speaker's own tags - "okay?",
# "right?" - not someone asking something. Anchoring a clip to one of those
# would be worse than not anchoring at all.
TAG_WORDS = {"okay", "ok", "right", "yeah", "yes", "no", "correct", "huh",
             "hmm", "true", "see", "know"}
INTERROGATIVES = {"what", "why", "how", "when", "where", "who", "whom", "whose",
                  "which", "do", "does", "did", "is", "are", "was", "were",
                  "can", "could", "should", "would", "will", "have", "has",
                  "tell", "explain", "walk", "describe", "give"}
LEAD_INS = {"so", "and", "but", "now", "then", "ok", "okay", "well"}
# "What was I doing?" is a speaker thinking aloud, not an interviewer asking.
# Opening a clip on one of those is worse than opening on the answer.
SELF_TALK = {"i", "me", "my", "myself"}


def question_text(text: str) -> str:
    """The question in this line, or "" if it is not really asking anything."""
    sentences = re.findall(r"[^.?!]*\?", text)
    if not sentences:
        return ""
    q = sentences[-1].strip().strip("\"'“”")
    words = re.findall(r"[a-zA-Z']+", q.lower())
    if len(words) < 3 or words[-1] in TAG_WORDS:
        return ""
    head = [w for w in words if w not in LEAD_INS][:1]
    if not head or head[0] not in INTERROGATIVES:
        return ""
    if SELF_TALK & set(words):
        return ""
    return q


def _open_on_question(clips: list[dict], segments: list[dict], max_s: int,
                      log) -> list[dict]:
    """Pull the setup question in front of an answer the model started cold.

    The prompt asks for question-then-answer, but models reliably trim to the
    answer, so this checks the transcript itself: if the seconds before a clip
    end in a question, the clip starts there instead.
    """
    added = 0
    for clip in clips:
        found = _question_start(segments, clip["start"], clip["end"], max_s)
        if found is None:
            continue
        start, asked = found
        clip["question"] = asked          # the question itself, not everything after it
        clip["start"] = start
        added += 1
    if added:
        log(f"  opened {added} clip(s) on the question that prompted them")
    return clips


def _question_start(segments: list[dict], clip_start: float, clip_end: float,
                    max_s: int) -> tuple[float, str] | None:
    before = [s for s in segments if s["end"] <= clip_start + 0.4 and s["text"].strip()]
    for i in range(len(before) - 1, -1, -1):
        seg = before[i]
        if clip_start - seg["start"] > QUESTION_LOOKBACK:
            return None                      # the setup is too far back to carry
        asked = question_text(seg["text"])
        if not asked:
            continue

        j = i                                # walk back to where the question began
        while j > 0:
            prev = before[j - 1]
            if (prev["text"].strip().endswith(SENTENCE_END)
                    or before[j]["start"] - prev["end"] > 0.8):
                break
            j -= 1

        start = before[j]["start"]
        if clip_start - start > QUESTION_LOOKBACK or clip_end - start > max_s:
            return None                      # too long to carry the question
        return start, asked
    return None


def _snap(clips: list[dict], words: list[dict], duration: float,
          min_s: int, max_s: int) -> list[dict]:
    """Nudge each boundary to the nearest word edge and clamp to legal lengths."""
    if not words:
        return clips

    starts = [w["start"] for w in words]
    ends = [w["end"] for w in words]
    out = []

    for c in clips:
        start = min(starts, key=lambda s: abs(s - c["start"]))
        end = min(ends, key=lambda e: abs(e - c["end"]))

        # 120ms of air either side so speech does not feel clipped
        start = max(0.0, start - 0.12)
        end = min(duration, end + 0.25)

        if end - start < min_s * 0.6:
            continue
        if end - start > max_s:
            end = start + max_s
        c["start"], c["end"] = round(start, 2), round(end, 2)
        out.append(c)
    return out


def _dedupe(clips: list[dict]) -> list[dict]:
    """Drop clips that overlap a stronger one by more than half their length."""
    kept = []
    for c in sorted(clips, key=lambda x: -x["virality"]):
        clash = False
        for k in kept:
            overlap = min(c["end"], k["end"]) - max(c["start"], k["start"])
            if overlap > 0.5 * min(c["end"] - c["start"], k["end"] - k["start"]):
                clash = True
                break
        if not clash:
            kept.append(c)
    return kept

"""Upload queue: which rendered clips have reached YouTube Shorts, and which haven't.

The registry is one JSON file so the dashboard, the API and n8n all read the
same truth. Clips are discovered from each job's clips.json, so nothing has to
be re-entered by hand after a render.

Lifecycle:
    new -> approved -> uploading -> uploaded
                          |
                          +------> failed -> (retry) approved
    new -> skipped
"""
import json
import os
import time

from . import config, pipeline

REGISTRY = config.DATA / "uploads.json"

NEW, APPROVED, UPLOADING, UPLOADED, FAILED, SKIPPED = (
    "new", "approved", "uploading", "uploaded", "failed", "skipped")
PENDING_STATES = (NEW, APPROVED, UPLOADING, FAILED)

# A stuck "uploading" means n8n claimed a clip and then died mid-run; after this
# long it goes back in the queue rather than blocking it forever.
CLAIM_TIMEOUT = 45 * 60

# A YouTube insert costs 1600 of the 10000 daily quota units. Clipper refuses to
# hand out a 7th clip in a day rather than letting n8n collect quotaExceeded.
DAY = 24 * 3600

YT_TITLE_MAX = 100
YT_DESC_MAX = 4900


def key(job_id: str, index: int) -> str:
    return f"{job_id}:{index}"


def _load() -> dict:
    try:
        reg = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        reg = {"clips": {}}
    return _flatten(reg)


def _flatten(reg: dict) -> dict:
    """Collapse the short-lived multi-platform rows back to YouTube only."""
    for row in reg.get("clips", {}).values():
        targets = row.pop("targets", None)
        if targets is None:
            continue
        yt = targets.get("youtube", {})
        status = yt.get("status", "waiting")
        row["status"] = {"waiting": NEW, "queued": APPROVED, "uploading": UPLOADING,
                         "uploaded": UPLOADED, "failed": FAILED,
                         "skipped": SKIPPED}.get(status, NEW)
        row["video_id"] = yt.get("media_id")
        row["error"] = yt.get("error")
        row["attempts"] = yt.get("attempts", 0)
        row["uploaded_at"] = yt.get("uploaded_at")
        row.pop("approved", None)
    return reg


def _save(reg: dict) -> None:
    reg["updated"] = time.time()
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    os.replace(tmp, REGISTRY)


def sync() -> dict:
    """Pick up clips rendered since the last look. Never touches existing rows."""
    reg = _load()
    clips = reg.setdefault("clips", {})
    added = 0

    for clips_file in sorted(config.DATA.glob("*/clips.json")):
        job_id = clips_file.parent.name
        job = pipeline.load(job_id)
        try:
            rendered = json.loads(clips_file.read_text())
        except (OSError, ValueError):
            continue
        for clip in rendered:
            k = key(job_id, clip["index"])
            if k in clips:
                continue
            clips[k] = {
                "job_id": job_id,
                "index": clip["index"],
                "source_title": job.title if job else "",
                "source_url": job.url if job else "",
                "file": clip["file"],
                "thumb": clip.get("thumb"),
                "hook": clip.get("hook", ""),
                "title": clip.get("title", ""),
                "reason": clip.get("reason", ""),
                "question": clip.get("question", ""),
                "custom_description": "",
                "virality": clip.get("virality", 0),
                "duration": clip.get("duration", 0),
                "status": NEW,
                "video_id": None,
                "error": None,
                "attempts": 0,
                "created": time.time(),
                "approved_at": None,
                "uploaded_at": None,
            }
            added += 1

    # A job deleted from disk shouldn't leave ghosts in the queue, but an
    # already-uploaded clip stays as a record of what went out.
    for k, row in list(clips.items()):
        if row["status"] != UPLOADED and not (config.DATA / row["job_id"]).is_dir():
            del clips[k]

    _reclaim(clips)
    _save(reg)
    return {"added": added, "total": len(clips)}


def _reclaim(clips: dict) -> None:
    now = time.time()
    for row in clips.values():
        if row["status"] == UPLOADING and now - row.get("claimed_at", 0) > CLAIM_TIMEOUT:
            row["status"] = FAILED
            row["error"] = "n8n claimed this clip but never reported back."


def all_rows() -> list[dict]:
    """Queue order: approved first, then by predicted virality."""
    reg = _load()
    rank = {APPROVED: 0, UPLOADING: 1, FAILED: 2, NEW: 3, UPLOADED: 4, SKIPPED: 5}
    return sorted(reg.get("clips", {}).values(),
                  key=lambda r: (rank.get(r["status"], 9), -r.get("virality", 0)))


def uploaded_today(clips: dict | None = None) -> int:
    rows = (clips or _load().get("clips", {})).values()
    cutoff = time.time() - DAY
    return sum(1 for r in rows if (r.get("uploaded_at") or 0) > cutoff)


def stats() -> dict:
    rows = all_rows()
    counts = {s: 0 for s in (NEW, APPROVED, UPLOADING, UPLOADED, FAILED, SKIPPED)}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "daily_limit": config.YT_DAILY_LIMIT,
        "uploaded_today": uploaded_today(),
        "total": len(rows),
        "uploaded": counts[UPLOADED],
        "queued": counts[APPROVED] + counts[UPLOADING],
        "pending_review": counts[NEW],
        "failed": counts[FAILED],
        "skipped": counts[SKIPPED],
        "not_uploaded": sum(counts[s] for s in PENDING_STATES),
    }


def set_status(job_id: str, index: int, status: str,
               description: str | None = None) -> dict:
    """Approving can carry a description; None leaves whatever is stored."""
    reg = _load()
    row = reg.get("clips", {}).get(key(job_id, index))
    if not row:
        raise KeyError("no such clip in the upload queue")
    if status == APPROVED and row.get("video_id"):
        raise ValueError("this clip is already on YouTube - re-queueing it "
                         "would post a duplicate")
    if description is not None:
        row["custom_description"] = description.strip()[:YT_DESC_MAX]
    row["status"] = status
    if status == APPROVED:
        row["approved_at"] = time.time()
        row["error"] = None
    if status == NEW:
        row["approved_at"] = None
        row["error"] = None
        row["video_id"] = None
    _save(reg)
    return row


def post_now(job_id: str, index: int, description: str | None = None) -> dict:
    """Approve this clip and put it at the front of the queue.

    The n8n workflow always asks for "the next clip", so jumping the queue is
    a property of the clip, not a different request: claim_next hands out
    anything flagged here before anything else. That keeps one workflow
    serving both the drip and an immediate post.
    """
    reg = _load()
    row = reg.get("clips", {}).get(key(job_id, index))
    if not row:
        raise KeyError("no such clip in the upload queue")
    if row.get("video_id"):
        raise ValueError("this clip is already on YouTube - posting it again "
                         "would be a duplicate")
    if row["status"] == UPLOADING:
        raise ValueError("this clip is being posted right now")

    if description is not None:
        row["custom_description"] = description.strip()[:YT_DESC_MAX]
    row.update(status=APPROVED, approved_at=time.time(), error=None,
               priority=time.time())
    _save(reg)
    return row


def capacity_left() -> int:
    return max(0, config.YT_DAILY_LIMIT - uploaded_today())


def approve_all() -> int:
    reg = _load()
    n = 0
    for row in reg.get("clips", {}).values():
        if row["status"] in (NEW, FAILED) and not row.get("video_id"):
            row["status"] = APPROVED
            row["approved_at"] = time.time()
            row["error"] = None
            n += 1
    _save(reg)
    return n


def claim_next(base_url: str) -> dict | None:
    """Hand n8n the next clip to upload and mark it in flight.

    Returns everything the workflow needs - the file URL plus finished YouTube
    metadata - so the n8n side stays a straight pipe with no expressions to edit.
    """
    reg = _load()
    clips = reg.get("clips", {})
    _reclaim(clips)

    # Already on YouTube: a lost result must never cause a second upload.
    for row in clips.values():
        if row["status"] in (APPROVED, UPLOADING) and row.get("video_id"):
            row["status"] = UPLOADED
            row["error"] = None
            row.setdefault("uploaded_at", time.time())

    if uploaded_today(clips) >= config.YT_DAILY_LIMIT:
        _save(reg)
        return {"empty": True, "reason": (
            f"daily cap reached ({config.YT_DAILY_LIMIT} uploads/24h) - "
            "YouTube quota allows no more today")}

    candidates = [r for r in clips.values() if r["status"] == APPROVED]
    if not candidates:
        _save(reg)
        return None
    # Anything sent with "Post now" goes first, oldest flag first; after that
    # it is simply the highest predicted score.
    row = max(candidates, key=lambda r: (1 if r.get("priority") else 0,
                                         -(r.get("priority") or 0),
                                         r.get("virality", 0),
                                         -r.get("created", 0)))
    row.pop("priority", None)

    row["status"] = UPLOADING
    row["claimed_at"] = time.time()
    row["attempts"] = row.get("attempts", 0) + 1
    _save(reg)

    return {
        **row,
        "key": key(row["job_id"], row["index"]),
        "file_url": f"{base_url}/api/jobs/{row['job_id']}/file/{row['file']}",
        "result_url": f"{base_url}/api/uploads/{row['job_id']}/{row['index']}/result",
        "youtube": youtube_meta(row),
    }


def youtube_meta(row: dict) -> dict:
    """Shorts metadata. #Shorts in the title is what gets it into the Shorts feed."""
    hook = (row.get("hook") or row.get("title") or "Clip").strip().rstrip(".")
    title = f"{hook} #Shorts"
    if len(title) > YT_TITLE_MAX:
        title = hook[:YT_TITLE_MAX - len(" #Shorts") - 1].rstrip() + "… #Shorts"

    custom = (row.get("custom_description") or "").strip()
    if custom:
        return {
            "title": title,
            "description": custom[:YT_DESC_MAX],
            "tags": ["shorts", "podcast", "mindset", "motivation"],
            "categoryId": "22",
            "privacyStatus": config.YT_PRIVACY,
            "selfDeclaredMadeForKids": False,
        }

    parts = [row.get("title") or hook]
    if row.get("question"):
        parts.append(f"Q: {row['question']}")
    if row.get("reason"):
        parts.append(row["reason"])
    if row.get("source_title"):
        src = f"From: {row['source_title']}"
        if row.get("source_url"):
            src += f"\n{row['source_url']}"
        parts.append(src)
    parts.append("#Shorts #podcast #mindset")

    return {
        "title": title,
        "description": "\n\n".join(parts)[:YT_DESC_MAX],
        "tags": ["shorts", "podcast", "mindset", "motivation"],
        "categoryId": "22",            # People & Blogs
        "privacyStatus": config.YT_PRIVACY,
        "selfDeclaredMadeForKids": False,
    }


def record_result(job_id: str, index: int, video_id: str | None,
                  error: str | None) -> dict:
    reg = _load()
    row = reg.get("clips", {}).get(key(job_id, index))
    if not row:
        raise KeyError("no such clip in the upload queue")
    if video_id:
        row.update(status=UPLOADED, video_id=video_id, error=None,
                   uploaded_at=time.time())
    elif row.get("video_id"):
        # n8n lost the id on the way back, but the video is already up.
        row.update(status=UPLOADED, error=None)
    elif not error:
        # The success branch reported with no id. It IS on YouTube - never call
        # this failed, or a retry posts a double.
        row.update(status=UPLOADED, uploaded_at=time.time(),
                   error="uploaded, but n8n did not return the video id "
                         "- republish the workflow to capture it")
    else:
        row.update(status=FAILED, error=error or "upload failed")
    row.pop("claimed_at", None)
    _save(reg)

    if row["status"] == UPLOADED and config.CLEAN_CLIP_AFTER_UPLOAD:
        from . import storage              # local: storage imports uploads
        storage.clean(job_id, working=False, posted=True)
    return row

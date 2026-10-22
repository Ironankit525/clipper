import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import config, pipeline, storage, uploads

async def catchup_loop() -> None:
    """Serve a drip slot the machine slept through.

    n8n's cron fires on schedule whether or not this laptop is awake to answer
    it; a sleeping Mac answers ECONNREFUSED and the slot is simply lost. This
    checks shortly after every wake, so the post goes out late rather than not
    at all. Each slot is marked served, so n8n and this never double-post.
    """
    while True:
        await asyncio.sleep(config.CATCHUP_EVERY)
        try:
            slot = uploads.due_slot()
            if not slot or not uploads.has_work():
                continue
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(config.N8N_WEBHOOK_URL,
                                         json={"source": "catchup", "slot": slot})
            if resp.status_code < 400:
                uploads.mark_slot(slot)
                print(f"[catchup] served missed slot {slot}", flush=True)
        except (httpx.HTTPError, OSError) as exc:
            print(f"[catchup] n8n unreachable: {exc}", flush=True)
        except Exception as exc:                    # never let the loop die
            print(f"[catchup] {exc!r}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """A restart used to abandon whatever was running. Pick it back up.

    Every stage caches its output, so a resumed job skips straight to wherever
    it got to - usually selection, the download and transcript being the slow
    parts. MAX_AUTO_RESUMES stops a job that crashes on startup looping.
    """
    if config.AUTO_RESUME:
        for job in list(JOBS.values()):
            if job.status != "interrupted" or job.resumes >= config.MAX_AUTO_RESUMES:
                continue
            job.resumes += 1
            job.log(f"Resuming after a restart (attempt {job.resumes}).")
            asyncio.create_task(asyncio.to_thread(pipeline.run, job))

    watcher = asyncio.create_task(catchup_loop()) if config.CATCHUP else None
    yield
    if watcher:
        watcher.cancel()


app = FastAPI(title="Clipper", lifespan=lifespan)
# Rehydrated from data/<id>/job.json so history survives restarts and --reload.
JOBS: dict[str, pipeline.Job] = pipeline.load_all()
STATIC = Path(__file__).parent / "static"


def job_dict(job: pipeline.Job) -> dict:
    return {"id": job.id, "url": job.url, "title": job.title, "status": job.status,
            "logs": job.logs, "clips": job.clips, "error": job.error,
            "progress": job.progress, "created": job.created, "updated": job.updated}


class ClipRequest(BaseModel):
    url: str
    n_clips: int = 6
    min_s: int = 20
    max_s: int = 60


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/config")
def get_config() -> dict:
    return {
        "select_provider": config.SELECT_PROVIDER,
        "transcribe_provider": config.TRANSCRIBE_PROVIDER,
        "problems": config.check_ready(),
    }


@app.post("/api/jobs")
async def create_job(req: ClipRequest) -> dict:
    problems = config.check_ready()
    if problems:
        raise HTTPException(400, "; ".join(problems))

    jid = pipeline.job_id(req.url + str(req.n_clips) + str(req.min_s) + str(req.max_s))
    existing = JOBS.get(jid)
    if existing and existing.status not in ("error", "interrupted"):
        return {"id": jid}

    job = pipeline.Job(id=jid, url=req.url, n_clips=req.n_clips,
                       min_s=req.min_s, max_s=req.max_s,
                       title=existing.title if existing else "",
                       logs=existing.logs if existing else [])
    JOBS[jid] = job
    asyncio.create_task(asyncio.to_thread(pipeline.run, job))
    return {"id": jid}


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    """Past and present runs, newest first."""
    for jid, job in pipeline.load_all().items():
        JOBS.setdefault(jid, job)
    return [j.summary() for j in sorted(JOBS.values(), key=lambda j: j.created, reverse=True)]


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict:
    job = JOBS.get(jid) or pipeline.load(jid)
    if not job:
        raise HTTPException(404, "no such job")
    JOBS.setdefault(jid, job)
    return job_dict(job)


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str) -> dict:
    job = JOBS.pop(jid, None) or pipeline.load(jid)
    if not job:
        raise HTTPException(404, "no such job")
    workdir = (config.DATA / jid).resolve()
    if workdir.parent == config.DATA.resolve() and workdir.is_dir():
        shutil.rmtree(workdir, ignore_errors=True)
    return {"deleted": jid}


@app.get("/api/jobs/{jid}/stream")
async def stream(jid: str) -> StreamingResponse:
    if jid not in JOBS:
        job = pipeline.load(jid)
        if job:
            JOBS[jid] = job

    async def events():
        sent = 0
        while True:
            job = JOBS.get(jid)
            if not job:
                break
            while sent < len(job.logs):
                yield f"data: {json.dumps({'log': job.logs[sent]})}\n\n"
                sent += 1
            yield f"data: {json.dumps({'status': job.status, 'title': job.title, 'clips': job.clips, 'error': job.error, 'progress': job.progress})}\n\n"
            if job.status in pipeline.TERMINAL:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(events(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Upload queue - the dashboard reads these, and so does the n8n workflow
# --------------------------------------------------------------------------- #
@app.get("/api/uploads")
def list_uploads() -> dict:
    uploads.sync()
    return {"stats": uploads.stats(), "clips": uploads.all_rows(),
            "privacy": config.YT_PRIVACY}


@app.get("/api/uploads/{jid}/{index}/preview")
def upload_preview(jid: str, index: int) -> dict:
    """What would be posted right now - used to show the default description."""
    row = next((r for r in uploads.all_rows()
                if r["job_id"] == jid and r["index"] == index), None)
    if not row:
        raise HTTPException(404, "no such clip in the upload queue")
    generated = uploads.youtube_meta({**row, "custom_description": ""})
    return {"title": generated["title"], "default_description": generated["description"],
            "custom_description": row.get("custom_description", "")}


@app.post("/api/uploads/sync")
def sync_uploads() -> dict:
    return uploads.sync()


@app.post("/api/uploads/approve-all")
def approve_all() -> dict:
    return {"approved": uploads.approve_all(), "stats": uploads.stats()}


@app.get("/api/uploads/next")
def next_upload() -> dict:
    """n8n pulls one clip per run. Empty means the queue is drained."""
    clip = uploads.claim_next(config.PUBLIC_URL)
    slot = uploads.due_slot()
    if slot:
        uploads.mark_slot(slot)        # whoever asked, this slot is served
    return clip or {"empty": True, "reason": "nothing approved"}


@app.post("/api/uploads/run-now")
async def run_now() -> dict:
    """Nudge the n8n workflow from the dashboard (server-side, so no CORS)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config.N8N_WEBHOOK_URL, json={"source": "dashboard"})
        return {"ok": resp.status_code < 400, "status": resp.status_code}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach n8n at {config.N8N_WEBHOOK_URL}: {exc}")


@app.post("/api/uploads/{jid}/{index}/result")
def upload_result(jid: str, index: int, body: dict = Body(default={})) -> dict:
    video_id = body.get("video_id") or body.get("media_id") or body.get("id")
    try:
        return uploads.record_result(jid, index, video_id, body.get("error"))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/uploads/{jid}/{index}/post-now")
async def post_now(jid: str, index: int, body: dict = Body(default={})) -> dict:
    """Approve this clip and ask n8n to post it immediately."""
    if not uploads.capacity_left():
        raise HTTPException(409, (
            f"daily cap reached ({config.YT_DAILY_LIMIT} uploads/24h). The clip "
            "stays queued and goes out on the next drip."))
    try:
        uploads.post_now(jid, index, body.get("description"))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(config.N8N_WEBHOOK_URL, json={"source": "post-now"})
    except httpx.HTTPError as exc:
        # It is queued first in line either way, so this is not a failure.
        return {"queued": True, "triggered": False,
                "detail": f"queued, but n8n did not answer ({exc}). "
                          "It will go out on the next scheduled run."}
    if resp.status_code >= 400:
        return {"queued": True, "triggered": False,
                "detail": f"queued, but n8n returned {resp.status_code}. "
                          "Is the workflow published?"}
    return {"queued": True, "triggered": True,
            "detail": "posting now - this takes about half a minute"}


@app.post("/api/uploads/{jid}/{index}/{action}")
def upload_action(jid: str, index: int, action: str,
                  body: dict = Body(default={})) -> dict:
    target = {"approve": uploads.APPROVED, "skip": uploads.SKIPPED,
              "unapprove": uploads.NEW, "retry": uploads.APPROVED}.get(action)
    if not target:
        raise HTTPException(400, f"unknown action {action!r}")
    # Only an approve carries a description; "" explicitly clears it back to
    # the generated one.
    description = body.get("description") if action == "approve" else None
    try:
        return uploads.set_status(jid, index, target, description)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# --------------------------------------------------------------------------- #
# Housekeeping - a 3-hour source is ~4GB, and most of it stops being useful
# the moment the clips exist
# --------------------------------------------------------------------------- #
@app.get("/api/storage")
def storage_overview() -> dict:
    return storage.overview()


@app.post("/api/storage/sweep")
def storage_sweep(source: bool = True, posted: bool = True) -> dict:
    """Clean every job: working files always, plus whatever is switched on."""
    return storage.sweep(source=source, posted=posted)


@app.post("/api/jobs/{jid}/cleanup")
def cleanup_job(jid: str, source: bool = True, posted: bool = True) -> dict:
    try:
        return storage.clean(jid, source=source, working=True, posted=posted)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/jobs/{jid}/file/{name}")
def get_file(jid: str, name: str) -> FileResponse:
    path = (config.DATA / jid / name).resolve()
    if not path.is_file() or config.DATA.resolve() not in path.parents:
        raise HTTPException(404, "not found")
    return FileResponse(path)

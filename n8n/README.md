# Shorts upload automation

Clipper keeps the queue; n8n does the posting. Clipper never holds a Google
credential — the OAuth token lives in n8n, where it belongs.

```
Clipper renders clips
        │
        ▼
  you approve in the dashboard        (clip becomes "queued")
        │
        ▼
  n8n: 9am / 2pm / 7pm                GET  /api/uploads/next   → claims 1 clip
        │                             GET  /api/jobs/…/clip.mp4 → binary
        ▼
  YouTube node uploads it             POST /api/uploads/…/result → video id
        │
        ▼
  dashboard shows "Uploaded ✓"
```

## 1. Google Cloud credential (once, ~5 minutes)

1. <https://console.cloud.google.com> → new project (any name).
2. **APIs & Services → Library → YouTube Data API v3 → Enable.**
3. **OAuth consent screen** → External → fill in app name + your email.
   Add yourself under **Test users** — without that, the token expires in 7 days.
   Add the scope `https://www.googleapis.com/auth/youtube.upload`.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
5. Authorised redirect URI — paste exactly what n8n shows you in step 2 below,
   normally `http://localhost:5678/rest/oauth2-credential/callback`.
6. Copy the client ID and client secret.

## 2. n8n credential

In n8n → **Credentials → New → YouTube OAuth2 API**. Paste the client ID and
secret, click **Connect my account**, pick the channel you want to post to.

## 3. Import the workflow

**Workflows → Import from File →** `n8n/clipper-shorts-upload.json`.

Open the **Upload to YouTube** node and select the credential you just made
(the import leaves it blank on purpose — credentials are never in the file).
Then **Activate** the workflow, top right.

## 4. Check it end to end

With one clip approved in the dashboard, hit **Upload next now**. That calls
Clipper, which pokes the n8n webhook; watch the run in n8n's **Executions** tab.

## The schedule

`0 9,14,19 * * *` — 3 uploads a day, one clip per run, highest predicted score
first. Edit the cron in the **Drip 3x daily** node to change it.

**Clipper's own cap is `YT_DAILY_LIMIT` in `.env` (10).** It is a brake, not the
real ceiling: a YouTube API upload costs 1,600 quota units against a project's
10,000 a day, which works out at 6. Once the project runs dry YouTube answers
`quotaExceeded`, Clipper marks that clip `failed`, and it can be retried the
next day. Raising the cap lets more through when quota allows; it cannot create
quota. A permanent increase needs a quota extension request to Google, which
involves an audit.

## If n8n says the connection was refused

`NodeApiError: The service refused the connection` on **Claim next clip**,
while `curl http://localhost:8000` works fine from a terminal, is an
IPv4/IPv6 split, not a dead server:

- macOS resolves `localhost` to `::1` first, then `127.0.0.1`
- curl and browsers try the second address when the first refuses; **Node,
  which n8n runs on, does not**
- so a server bound only to `127.0.0.1` is invisible to n8n

Clipper's launchd agent therefore binds `::1`. Running it by hand, use
`--host ::1` too, or point the workflow at `http://127.0.0.1:8000` instead of
`localhost`.

## When the machine is asleep

n8n's cron fires whether or not the Mac is awake to answer it, and a laptop in
deep sleep answers nothing — the slot is burned on a failed run. Clipper keeps
its own watch (`CATCHUP`, every `CATCHUP_EVERY` seconds): if a drip hour in
`DRIP_HOURS` has passed unserved and clips are queued, it pokes the webhook
itself, so the post goes out late rather than never. Whichever side serves a
slot marks it, so they never double-post.

## Endpoints the workflow uses

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/uploads/next` | Claim the top approved clip. Marks it `uploading`; returns `{"empty": true}` when the queue is drained. |
| `GET` | `/api/jobs/{job}/file/{clip}` | The mp4 itself. |
| `POST` | `/api/uploads/{job}/{index}/result` | `{"video_id": "..."}` or `{"error": "..."}`. |

A clip stuck in `uploading` for 45 minutes (n8n crashed mid-run) is released
back to the queue as `failed`, so one bad execution can't block the pipeline.

# Clipper

Paste a YouTube URL, get back N vertical shorts — cut at sensible boundaries,
reframed to 9:16, captioned word-by-word, and ranked by how likely each is to
perform.

Clip selection goes to an API you hold the key for. Everything else is local:
ffmpeg does the video work, YuNet (a small bundled DNN) finds faces for
reframing, and transcription can be a local whisper or an API, your choice.

```
YouTube URL
   ↓  yt-dlp                     download the source
   ↓  whisper / Gemini           transcript with word-level timings
   ↓  Gemini / GPT               pick the N strongest moments, question first
   ↓  YuNet + ffmpeg             follow the speaker, crop 9:16, burn captions
   ↓  dashboard                  review, edit the description, approve
   ↓  n8n + YouTube Data API     drip-post them, a few a day
```

Built to run unattended on one machine: Clipper owns the queue and the files,
a local n8n instance holds the OAuth token and does the posting.

## Setup

Homebrew's plain `ffmpeg` is built without libass, so it cannot burn in
captions. You need the full build:

```bash
brew install ffmpeg-full yt-dlp
```

It installs keg-only, and the app finds it on its own. Then:

```bash
cp .env.example .env    # then paste your key in
```

Set `SELECT_PROVIDER` to `gemini` or `openai` and paste that key in.

`TRANSCRIBE_PROVIDER` is the one real choice:

| value | needs | word timing | cost/hour |
|---|---|---|---|
| `gemini` | Gemini key | interpolated inside segments — good for cutting, slightly loose on captions | ~$0.03 |
| `local` | nothing (faster-whisper, already installed) | true per-word | free, ~2-4 min CPU |
| `openai` | OpenAI key | true per-word | ~$0.36 |

Default is `gemini`, so nothing runs on your machine. If captions ever look
out of sync with the audio, switch that one line to `local`.

`MAX_HEIGHT` (default `1080`) caps the source download. A multi-hour podcast is
several GB at 1080p; `MAX_HEIGHT=720` cuts that to about a third, at a small
cost in crop sharpness.

## Run

```bash
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

### Running it as a service

`deploy/com.clipper.app.plist.example` is a launchd agent: replace the paths,
drop it in `~/Library/LaunchAgents/`, and Clipper starts at login and restarts
if it crashes. An interrupted job resumes itself on the next start, since every
stage caches its output.

### Starting and stopping

Clipper runs as a launchd agent, so it survives closing the terminal and comes
back at login. That also means `kill` will not stop it - launchd restarts it
within seconds. Use the `clipper` script:

```bash
./clipper status      # running? on what pid? does the page answer?
./clipper stop        # stop it, and stop launchd putting it back
./clipper start       # start it again
./clipper restart     # pick up code changes
./clipper log         # tail data/server.log
./clipper disable     # stay stopped across logout and reboot
./clipper enable      # start at login again
./clipper uninstall   # remove the service entirely
```

A `stop` lasts until you `start` again or log out and back in; `disable`
survives a reboot.

## Question, then answer

For interviews the selector is told to open a clip on the question that
prompted the answer, not on the answer alone: the question is the tension the
answer pays off, and without it a viewer lands mid-conversation. Models trim to
the answer anyway, so `_open_on_question` in `app/select.py` checks the
transcript itself - if the seconds before a chosen clip end in a question, the
clip is extended back to the first word of that question.

It only does this when the question ends within `QUESTION_MAX_GAP` seconds of
the answer and the whole thing still fits inside Max secs. Long Q&A pairs
therefore need a larger Max secs than a bare soundbite would - 75-90s is a
reasonable ceiling now that Shorts allows three minutes.

## Posting: queue it, or send it now

Each clip has two ways out:

- **Approve** puts it in the queue. n8n takes the highest-scoring queued clip at
  9am, 2pm and 7pm.
- **Post now** approves it *and* asks n8n to post that clip immediately,
  description included.

"Post now" does not need a second workflow. It flags the clip, and
`claim_next` hands out flagged clips before anything else, so the same workflow
serves both paths - it just gets asked sooner. If n8n is unreachable or the
workflow is unpublished, the clip stays first in line and goes out on the next
drip; the dashboard says which happened.

The daily cap still applies. With the 6/day quota ceiling already reached,
Post now refuses rather than letting the upload fail at YouTube.

## Descriptions

A clip waiting for review has a description box. Type something and hit
**Approve** and that text is posted verbatim — links included. Leave it blank
and Clipper builds one from the clip's own title, the question it opens on, why
the model picked it, and a credit back to the source video.

**Show the generated description** fills the box with the default so you can
edit it rather than start from nothing. Clearing the box and approving again
goes back to the generated one. To change a description after approving,
**Unqueue** the clip first.

The title is always generated (hook + `#Shorts`) — only the description is
yours to override.

## Disk space

A three-hour source is about 4GB, and most of a finished job stops being useful
the moment the clips exist. **Previous runs** shows what each run occupies and
offers a *Free …* button; the bar above the list cleans every run at once.

| | deleted | when |
|---|---|---|
| working files | `audio.mp3`, `*.ass`, download leftovers | automatically, as soon as a job renders |
| posted clips | `clip_NN.mp4` already on YouTube | automatically (`CLEAN_CLIP_AFTER_UPLOAD`) |
| source video | `source.mp4` | on the button, or `CLEAN_SOURCE_AFTER_RENDER=true` |

Always kept: `transcript.json` (so clips can be re-selected without paying to
transcribe again), the thumbnails the dashboard shows, `clips.json` and
`job.json`. **A clip still waiting to upload is never deleted** - only clips the
registry records as posted.

Deleting the source is the one irreversible call: re-rendering that run later
means downloading the video again. The `✕` on a run still deletes everything.

## How the camera is framed

Every clip is a 9:16 window cut out of a 16:9 frame, so something has to decide
where that window sits. Faces are sampled four times a second with YuNet (a
small DNN; weights in `models/`, downloaded once from OpenCV's model zoo). If
the weights are missing it falls back to Haar cascades, which cannot see a face
in profile - most of a podcast - so keep the file.

The window then behaves like an operator rather than a tracker: it **holds**
while the subject moves inside a dead band, **follows** with an eased move when
they leave it, and **cuts** when the position jumps, which means the source
itself cut to another angle. ffmpeg evaluates the path per frame, so moves are
continuous instead of stepping between keyframes.

The weights in `models/` are YuNet from OpenCV's model zoo (Apache-2.0),
bundled so a clone works offline.

If the tracking ever misbehaves on a particular video, the knobs are at the top
of `app/reframe.py`: `DEADBAND` (how far the subject roams free), `MIN_MOVE_GAP`
(how often the frame may reposition) and `PAN_SPEED`.

## Posting to YouTube Shorts

The page's **Publishing queue** shows every rendered clip and how far it has
got: needs review → queued → posted, with a count of each. Approve a clip and a
locally-installed n8n workflow posts it on a 3-a-day drip. Setup is in
[`n8n/README.md`](n8n/README.md); the importable workflow sits beside it.

Clipper holds no Google credential — it owns the queue, n8n holds the OAuth
token and does the upload.

Every run is written to `data/<job-id>/job.json` as it goes, so **Previous
runs** on the page survives a restart. Click a past run to pull its logs and
clips back up; re-running one that stopped part-way picks up from whatever is
already on disk (the download and the transcript are both cached). Run it from
a terminal you intend to keep open, or redirect its output to a file — the
server is still a foreground process.

## How it works

1. **download** — `yt-dlp` pulls 1080p, ffmpeg strips a 32kbps mono audio track
2. **transcribe** — word-level timestamps; cached per video so re-runs are free
3. **select** — the transcript goes to your LLM with an editor's brief: must be
   self-contained, must hook in 3 seconds, must pay off. Returns start/end, an
   on-screen hook, a post caption, a 0-100 score and a one-line reason.
   Boundaries are then snapped to real word edges and overlaps are dropped.
4. **reframe** — faces sampled twice a second, path smoothed both directions.
   Static subject gets one fixed crop; a moving one gets a tracked crop.
5. **caption** — an ASS subtitle file, 2-3 words at a time, active word popped
   in amber, burned in during encode
6. **render** — one ffmpeg pass per clip using the M-series hardware encoder

## Cost per hour of video

- OpenAI whisper-1 transcription: ~$0.36
- Gemini 2.5 Flash selection: ~$0.02
- Everything else: free

## Tuning

- Caption look — `app/captions.py`, the `Caption` style line and `HIGHLIGHT`
  (font is Arial Black; any font fontconfig can see will work)
- What counts as viral — the `SYSTEM` prompt in `app/select.py`
- Camera steadiness — `SMOOTH_ALPHA` in `app/reframe.py` (lower is lazier)

## Known limits

- Face tracking picks the largest face, not the one currently speaking. On a
  two-person interview it will sometimes hold on the listener. Fixing that
  properly needs audio-visual active-speaker detection.
- Scoring is a language model's judgement, not engagement data. Treat the
  ranking as a shortlist to pick from, not a verdict.
- With `TRANSCRIBE_PROVIDER=gemini`, word times are interpolated across each
  segment, so captions can drift a little inside long sentences. `local` fixes
  it.
- Downloading videos you do not own is against YouTube's terms, whatever tool
  does it.


## Checking it still works

`smoketest.py` renders a synthetic clip (captions, both crop modes, encoder)
with no network or API key. `fulltest.py` runs the whole chain on a real
19-second YouTube video with clip selection stubbed out.

```bash
.venv/bin/python smoketest.py
```

"""End-to-end on a real video, with clip selection stubbed (no API key needed)."""
import os
from pathlib import Path

os.environ["TRANSCRIBE_PROVIDER"] = "local"
os.environ["LOCAL_WHISPER_MODEL"] = "tiny.en"

from app import config
config.TRANSCRIBE_PROVIDER = "local"
config.LOCAL_WHISPER_MODEL = "tiny.en"

from app import captions, download, reframe, render, transcribe

WORK = Path("data/ingest_test")
log = lambda m: print("   ", m)

video, audio, meta = download.download(
    "https://www.youtube.com/watch?v=jNQXAC9IVRw", WORK, log)

tr = transcribe.transcribe(audio, WORK, log)
print(f"\n[1] transcript: {len(tr['words'])} words, {len(tr['segments'])} segments")
print("   text:", " ".join(w["word"].strip() for w in tr["words"])[:160])
assert tr["words"], "no words came back"

pick = {"start": 0.0, "end": min(15.0, meta["duration"]),
        "hook": "THE FIRST YOUTUBE VIDEO", "virality": 77}
words = [{"word": w["word"], "start": w["start"] - pick["start"],
          "end": w["end"] - pick["start"]}
         for w in tr["words"] if w["end"] > pick["start"] and w["start"] < pick["end"]]
print(f"\n[2] clip words: {len(words)}")

ass = captions.build(words, pick["hook"], pick["end"], WORK / "real.ass")
plan = reframe.plan(video, pick["start"], pick["end"], meta["width"], meta["height"], log)
print(f"\n[3] crop: {plan['mode']} w={plan['crop_w']} x={plan.get('x')}")

out = render.render(video, pick["start"], pick["end"], ass, plan,
                    WORK / "real.mp4", WORK, log)
info = download.probe(out)
print(f"\n[4] rendered {info['width']}x{info['height']} {info['duration']:.1f}s "
      f"{out.stat().st_size//1000}KB")
assert (info["width"], info["height"]) == (1080, 1920)
print("\nEND-TO-END PASS (selection stubbed)")

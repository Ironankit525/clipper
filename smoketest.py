"""Exercise captions + crop + encode on a synthetic source, no API key needed."""
from pathlib import Path

from app import captions, download, reframe, render

WORK = Path("data/smoketest")
WORK.mkdir(parents=True, exist_ok=True)
SRC = WORK / "source.mp4"
if not SRC.exists():
    import subprocess
    from app import config
    subprocess.run([
        config.FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
        "-t", "10", "-c:v", "libx264", "-c:a", "aac", str(SRC)
    ], check=True, capture_output=True)

def log(msg):
    print("   ", msg)


meta = download.probe(SRC)
print(f"source: {meta['width']}x{meta['height']} {meta['duration']:.1f}s @ {meta['fps']:.0f}fps")

text = ("This is the part where everything changed. "
        "Nobody tells you that the first year is the hardest. "
        "And then it clicked.")
words, t = [], 0.3
for token in text.split():
    dur = 0.18 + 0.035 * len(token)
    words.append({"word": token, "start": round(t, 3), "end": round(t + dur, 3)})
    t += dur + 0.04
print(f"fake transcript: {len(words)} words spanning {words[-1]['end']:.1f}s")

# 1. captions
ass = captions.build(words, "NOBODY TELLS YOU THIS", 10.0, WORK / "test.ass")
body = ass.read_text()
events = [l for l in body.splitlines() if l.startswith("Dialogue")]
print(f"\n[1] captions: {len(events)} events for {len(words)} words")
assert len(events) >= len(words), "every word needs its own event"
print("   sample:", events[2][:120])

# 2. crop plan on a face-less source -> should centre
plan = reframe.plan(SRC, 0.0, 10.0, meta["width"], meta["height"], log)
print(f"\n[2] crop plan: {plan['mode']} crop_w={plan['crop_w']} x={plan.get('x')}")
assert plan["crop_w"] == 608, f"9:16 of 1080-high should be 608 wide, got {plan['crop_w']}"
assert plan["mode"] in ("static", "track")

# 3. render with the static plan
out = render.render(SRC, 0.0, 10.0, ass, plan, WORK / "static.mp4", WORK, log)
info = download.probe(out)
print(f"\n[3] static render: {info['width']}x{info['height']} {info['duration']:.1f}s "
      f"({out.stat().st_size / 1e6:.1f}MB)")
assert (info["width"], info["height"]) == (1080, 1920), "output must be 1080x1920"

# 4. render with a tracked plan (sendcmd path)
track = {**plan, "mode": "track",
         "path": [(i * 0.5, int(200 + 500 * (i / 20))) for i in range(21)],
         "x": 200}
out2 = render.render(SRC, 0.0, 10.0, ass, track, WORK / "tracked.mp4", WORK, log)
info2 = download.probe(out2)
print(f"\n[4] tracked render: {info2['width']}x{info2['height']} {info2['duration']:.1f}s "
      f"({out2.stat().st_size / 1e6:.1f}MB)")
assert (info2["width"], info2["height"]) == (1080, 1920)

# 5. thumbnail
thumb = render.thumbnail(out, WORK / "thumb.jpg")
print(f"\n[5] thumbnail: {thumb.stat().st_size // 1000}KB")
assert thumb.stat().st_size > 2000

print("\nALL PASS")

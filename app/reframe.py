"""Work out where the 9:16 window should sit inside a wider frame.

A camera operator does not twitch. They hold a frame, follow a subject who
drifts, and cut when the shot genuinely changes. This plans the crop the same
way, in three stages:

  lock on   one subject per sampled frame, with hysteresis so a second face
            cannot steal the frame by being briefly larger
  split     the path wherever it jumps - a jump between two samples is the
            source cutting to another angle, not a person moving
  describe  each shot as either a fixed frame or a slow follow, and let ffmpeg
            evaluate the result per frame

What made the old output jitter was emitting a crop position twice a second and
letting ffmpeg step between them: 2Hz staircase motion. Nothing steps now -
holds are constant, follows are continuous, and cuts are instant on purpose.
"""
import json
import subprocess
import sys
from pathlib import Path

CUT_JUMP = 0.35           # of crop width between samples: that was a cut
STILL_TRAVEL = 0.06       # of source width: a shot this steady gets one frame
SHOT_MIN = 1.5            # seconds; shorter than this is not a shot
SWITCH_HOLD = 0.75        # seconds another face must lead before we follow it
DEADBAND = 0.18           # of crop width: the subject can move this much free
MIN_MOVE_GAP = 2.0        # seconds between repositions, so it cannot oscillate
SETTLE = 0.75             # seconds of lookahead, so we chase a move not a lean
PAN_SPEED = 320.0         # px/sec: a deliberate reposition
PAN_MIN, PAN_MAX = 0.35, 1.2
MAX_CUTS = 8              # beyond this the reframe is fighting the source edit
SMOOTH_WINDOW = 5         # samples in the median filter (~1.25s at 4fps)
SMOOTH_ALPHA = 0.25


def plan(video: Path, start: float, end: float, src_w: int, src_h: int, log) -> dict:
    crop_w = round(src_h * 9 / 16 / 2) * 2
    if crop_w >= src_w:
        return {"mode": "none", "crop_w": src_w, "crop_h": src_h}

    base = {"crop_w": crop_w, "crop_h": src_h}
    max_x = src_w - crop_w
    samples = _scan(video, start, end, log)

    def to_x(center: float) -> int:
        return int(min(max(round(center - crop_w / 2), 0), max_x))

    if not samples or not any(faces for _, faces in samples):
        return {**base, "mode": "static", "x": max_x // 2}

    track = _lock_on(samples)
    shots = _shots(track, crop_w)
    keys = _keys(shots, src_w, to_x, end - start)

    cuts = sum(1 for k in keys[1:] if k[2] == 0)
    if len(keys) == 1 or cuts > MAX_CUTS:
        if cuts > MAX_CUTS:
            log(f"  {cuts} shot changes - holding one frame instead")
        return {**base, "mode": "static", "x": keys[0][1]}

    follows = sum(1 for k in keys[1:] if k[2] > PAN_MAX)
    log(f"  camera: {len(shots)} shot(s), {cuts} cut(s), "
        f"{len(keys) - 1 - cuts - follows} pan(s), {follows} follow key(s)")
    return {**base, "mode": "track", "keys": keys, "x": keys[0][1]}


def _scan(video: Path, start: float, end: float, log) -> list:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.facescan", str(video), str(start), str(end)],
            capture_output=True, text=True, timeout=600,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if proc.returncode != 0:
            log("  face scan unavailable, centring crop")
            return []
        return json.loads(proc.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        log("  face scan failed, centring crop")
        return []


def _lock_on(samples: list) -> list[tuple[float, float]]:
    """One subject per frame. Size decides, continuity breaks ties.

    In a two-shot both faces are much the same size, so without the continuity
    term the frame would flip between them every few samples.
    """
    locked = None
    rival_since = None
    track = []

    for t, faces in samples:
        if faces:
            if locked is None:
                locked = max(faces, key=lambda f: f[1])[0]
            near = min(faces, key=lambda f: abs(f[0] - locked))
            biggest = max(faces, key=lambda f: f[1])

            if biggest[0] == near[0] or biggest[1] < near[1] * 1.25:
                locked, rival_since = near[0], None
            else:
                rival_since = t if rival_since is None else rival_since
                if t - rival_since >= SWITCH_HOLD:
                    locked, rival_since = biggest[0], None
                else:
                    locked = near[0]
        if locked is not None:
            track.append((t, locked))
    return track


def _shots(track: list[tuple[float, float]], crop_w: int) -> list[list]:
    """Split where the subject teleports; that is the source cutting."""
    shots, current = [], [track[0]]
    for prev, point in zip(track, track[1:]):
        if abs(point[1] - prev[1]) > CUT_JUMP * crop_w:
            shots.append(current)
            current = [point]
        else:
            current.append(point)
    shots.append(current)

    merged = [shots[0]]
    for shot in shots[1:]:
        if shot[-1][0] - shot[0][0] < SHOT_MIN:
            merged[-1].extend(shot)         # a blink, not a shot
        else:
            merged.append(shot)
    if len(merged) > 1 and merged[0][-1][0] - merged[0][0][0] < SHOT_MIN:
        head = merged.pop(0)
        merged[0] = head + merged[0]
    return merged


def _keys(shots: list, src_w: float, to_x, duration: float
          ) -> list[tuple[float, int, float]]:
    """(time, x, seconds to get there); 0 seconds means cut.

    Dead-band follow: the frame holds while the subject moves inside a comfort
    zone, and only re-centres once they leave it. That is what a human
    operator does, and it is why the result reads as still rather than swimmy.
    """
    keys: list[tuple[float, int, float]] = []
    band = DEADBAND * _crop_width(src_w)

    for shot in shots:
        times = [t for t, _ in shot]
        xs = _smooth([c for _, c in shot])
        centre = _median(xs[:max(1, int(SETTLE * 4))])
        last_move = -MIN_MOVE_GAP

        if not keys:
            keys.append((0.0, to_x(centre), 0.0))
        else:
            t0 = round(max(times[0], keys[-1][0] + 0.1), 2)
            keys.append((t0, to_x(centre),
                         _transition(keys[-1][1], to_x(centre), src_w)))

        for i, (t, x) in enumerate(zip(times, xs)):
            if abs(x - centre) <= band or t - last_move < MIN_MOVE_GAP:
                continue
            ahead = xs[i:i + max(1, int(SETTLE * 4))]
            target = _median(ahead)
            if abs(target - centre) <= band:
                continue
            move = _transition(to_x(centre), to_x(target), src_w)
            arrive = round(t + max(move, 0.05), 2)
            if arrive <= keys[-1][0] + 0.2:
                continue
            if arrive > duration - 0.3:
                break                       # no time left to land the move
            keys.append((arrive, to_x(target), move))
            centre, last_move = target, t
    return keys


def _crop_width(src_w: float) -> float:
    """Crop width for a 9:16 window in a 16:9 frame."""
    return src_w * (9 / 16) * (9 / 16)


def _transition(x0: int, x1: int, src_w: float) -> float:
    """Seconds to reposition between shots, or 0 meaning cut."""
    dist = abs(x1 - x0)
    if dist > CUT_JUMP * src_w * 9 / 16:
        return 0.0
    return min(max(dist / PAN_SPEED, PAN_MIN), PAN_MAX)


def _smooth(values: list[float]) -> list[float]:
    """A median filter kills single-frame outliers; a zero-phase EMA removes
    the rest without the lag a one-way filter would add."""
    n = len(values)
    half = SMOOTH_WINDOW // 2
    out = [_median(values[max(0, i - half):min(n, i + half + 1)]) for i in range(n)]
    for i in range(1, n):
        out[i] = SMOOTH_ALPHA * out[i] + (1 - SMOOTH_ALPHA) * out[i - 1]
    for i in range(n - 2, -1, -1):
        out[i] = SMOOTH_ALPHA * out[i] + (1 - SMOOTH_ALPHA) * out[i + 1]
    return out


def _median(values: list[float]) -> float:
    return sorted(values)[len(values) // 2]


def crop_x_expr(keys: list[tuple[float, int, float]]) -> str:
    """An ffmpeg expression for crop's x, evaluated per frame.

    Holds are constant, cuts are instant, and everything else smoothsteps
    (3u^2-2u^3) - zero velocity at both ends, which is the difference between
    a camera move and a slide.
    """
    expr = str(keys[-1][1])
    for (t0, x0, _), (t1, x1, move) in zip(reversed(keys[:-1]), reversed(keys[1:])):
        if not move:
            expr = f"if(lt(t,{t1:.2f}),{x0},{expr})"
            continue
        start = max(t1 - move, t0)
        dur = max(t1 - start, 0.1)
        u = f"((t-{start:.2f})/{dur:.2f})"
        ease = f"{x0}+({x1 - x0})*({u}*{u}*(3-2*{u}))"
        expr = f"if(lt(t,{start:.2f}),{x0},if(lt(t,{t1:.2f}),{ease},{expr}))"
    return expr

"""Face sampler, run as its own process.

OpenCV and faster-whisper's `av` each bundle their own libavdevice; importing
both into one process triggers an ObjC duplicate-class warning and can crash.
Keeping cv2 behind a subprocess boundary sidesteps that entirely.

Detection is YuNet (a small DNN shipped with OpenCV, weights in models/). The
Haar cascade it replaces could not see a face in profile, which is most of a
podcast: it returned nothing for whole stretches and fired on dark background
instead, so the crop was being aimed by noise. Haar stays as a fallback for
when the model file is absent.

Frames are walked in order and only sampled frames are decoded to BGR (grab()
skips the rest), which is both faster and more accurate than seeking per sample.

Usage: python -m app.facescan <video> <start> <end>
   ->  JSON [[t, [[center_x, width], ...]], ...]   faces per sampled frame
"""
import json
import sys
from pathlib import Path

import cv2

SAMPLE_FPS = 4.0          # denser than the eye needs, so smoothing has data
DETECT_WIDTH = 960.0      # detector input; coordinates are scaled back to source
MIN_SCORE = 0.7           # YuNet confidence below this is usually furniture
MODEL = Path(__file__).resolve().parent.parent / "models" / \
    "face_detection_yunet_2023mar.onnx"


class Detector:
    """YuNet if the weights are there, Haar frontal+profile if not."""

    def __init__(self, width: int, height: int):
        self.yunet = None
        if MODEL.exists():
            try:
                self.yunet = cv2.FaceDetectorYN.create(
                    str(MODEL), "", (width, height), MIN_SCORE, 0.3, 5000)
            except cv2.error:
                self.yunet = None
        if self.yunet is None:
            self.cascades = [
                cv2.CascadeClassifier(cv2.data.haarcascades + name)
                for name in ("haarcascade_frontalface_default.xml",
                             "haarcascade_profileface.xml")
            ]

    def __call__(self, frame, scale: float) -> list[list[float]]:
        if self.yunet is not None:
            _, faces = self.yunet.detect(frame)
            return [[round(float(x + w / 2) / scale, 1), round(float(w) / scale, 1)]
                    for x, y, w, h, *_rest, score in (faces if faces is not None else [])
                    if score >= MIN_SCORE]

        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        out = []
        for cascade in self.cascades:
            if cascade.empty():
                continue
            for fx, fy, fw, fh in cascade.detectMultiScale(gray, 1.15, 6,
                                                           minSize=(40, 40)):
                out.append([round((fx + fw / 2) / scale, 1), round(fw / scale, 1)])
            gray = cv2.flip(gray, 1)         # profile cascade only sees one way
        return out


def main() -> None:
    video, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        json.dump([], sys.stdout)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920.0
    scale = min(1.0, DETECT_WIDTH / src_w)
    detect = Detector(int(src_w * scale),
                      int((cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080.0) * scale))

    stride = max(1, int(round(fps / SAMPLE_FPS)))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)

    out = []
    i = 0
    while True:
        if start + i / fps > end:
            break
        if i % stride:
            if not cap.grab():          # skip cheaply, no colour conversion
                break
            i += 1
            continue

        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1.0 else frame
        out.append([round(i / fps, 3), detect(small, scale)])
        i += 1

    cap.release()
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()

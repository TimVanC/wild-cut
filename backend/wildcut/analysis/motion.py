"""Subject-motion scoring from dense optical flow with global camera motion removed.

The curve is sampled at `sample_fps` (default 10) on a downscaled proxy. For each pair of
consecutive samples we compute Farneback flow, subtract the median flow vector (camera pan/
tilt), and summarize the residual magnitude. Units: fraction of frame width per second, so
values are comparable across clips of different resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

SAMPLE_FPS = 20.0
ANALYSIS_WIDTH = 320


@dataclass
class MotionCurve:
    times: list[float]
    subject: list[float]          # residual (subject) motion, width-fractions per second
    camera: list[float]           # magnitude of global motion, same units
    boxes: list[list[float] | None]  # normalized [x, y, w, h] of the largest moving region, or None
    peak_times: list[float] = field(default_factory=list)
    peak_values: list[float] = field(default_factory=list)
    smoothed: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MotionCurve":
        return cls(**{k: d.get(k, [] if k != "boxes" else []) for k in cls.__dataclass_fields__})

    def value_at(self, t: float) -> float:
        if not self.times:
            return 0.0
        return float(np.interp(t, self.times, self.smoothed or self.subject))

    def box_at(self, t: float) -> list[float] | None:
        if not self.times:
            return None
        i = int(np.argmin(np.abs(np.asarray(self.times) - t)))
        return self.boxes[i]


def _largest_moving_box(mag: np.ndarray, thresh: float) -> list[float] | None:
    import cv2

    mask = (mag > thresh).astype(np.uint8)
    if mask.sum() < 12:
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = stats[idx]
    if area < 12:
        return None
    H, W = mag.shape
    return [float(x / W), float(y / H), float(w / W), float(h / H)]


def compute_motion(path: str | Path, sample_fps: float = SAMPLE_FPS, width: int = ANALYSIS_WIDTH,
                   start: float = 0.0, end: float | None = None) -> MotionCurve:
    import cv2
    from wildcut.media import iter_frames

    times: list[float] = []
    subject: list[float] = []
    camera: list[float] = []
    boxes: list[list[float] | None] = []
    prev = None
    prev_t = None
    for t, frame in iter_frames(path, sample_fps=sample_fps, width=width, start=start, end=end, gray=True):
        if prev is not None:
            dt = max(1e-3, t - prev_t)
            flow = cv2.calcOpticalFlowFarneback(prev, frame, None, 0.5, 4, 21, 3, 5, 1.2, 0)
            # global camera motion = median flow over the frame interior
            h, w = flow.shape[:2]
            m = int(0.08 * min(h, w))
            inner = flow[m:h - m, m:w - m]
            med = np.median(inner.reshape(-1, 2), axis=0)
            resid = inner - med
            mag = np.sqrt(resid[..., 0] ** 2 + resid[..., 1] ** 2)
            # robust summary: mean of the top 4% magnitudes (subject is small vs background)
            k = max(16, int(0.04 * mag.size))
            top = np.partition(mag.reshape(-1), mag.size - k)[-k:]
            subj = float(top.mean()) / w / dt
            cam = float(np.hypot(med[0], med[1])) / w / dt
            box = _largest_moving_box(mag, max(0.6, 0.35 * float(top.max())))
            if box is not None:
                # convert from interior coords back to full-frame normalized coords
                box = [(box[0] * (w - 2 * m) + m) / w, (box[1] * (h - 2 * m) + m) / h,
                       box[2] * (w - 2 * m) / w, box[3] * (h - 2 * m) / h]
            times.append(round(float(t), 4))
            subject.append(subj)
            camera.append(cam)
            boxes.append(box)
        prev, prev_t = frame, t
    curve = MotionCurve(times, subject, camera, boxes)
    finalize_curve(curve, sample_fps)
    return curve


def finalize_curve(curve: MotionCurve, sample_fps: float = SAMPLE_FPS, min_gap: float = 0.5) -> None:
    """Smooth the subject curve and find prominent peaks (in place)."""
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks

    if len(curve.subject) < 3:
        curve.smoothed = list(curve.subject)
        curve.peak_times, curve.peak_values = [], []
        return
    s = np.asarray(curve.subject, dtype=np.float64)
    sm = gaussian_filter1d(s, sigma=0.8, mode="nearest")
    curve.smoothed = [float(v) for v in sm]
    base = float(np.median(sm))
    top = float(sm.max())
    if top <= 1e-6:
        curve.peak_times, curve.peak_values = [], []
        return
    prominence = max(0.03, 0.25 * (top - base))
    distance = max(1, int(round(min_gap * sample_fps)))
    idx, props = find_peaks(sm, prominence=prominence, distance=distance, height=base + 0.5 * (top - base) * 0.5)
    # refine each peak time to the raw (unsmoothed) maximum within +-1 sample
    pts, pvs = [], []
    for i in idx:
        lo, hi = max(0, i - 1), min(len(s) - 1, i + 1)
        j = lo + int(np.argmax(s[lo:hi + 1]))
        pts.append(curve.times[j])
        pvs.append(float(sm[i]))
    order = np.argsort(pvs)[::-1]
    curve.peak_times = [pts[i] for i in order]
    curve.peak_values = [pvs[i] for i in order]

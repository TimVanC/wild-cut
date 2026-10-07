"""Subject tracking: a smoothed crop path per moment so reframing keeps the animal in frame.

A crop path is a list of {t, cx, cy} with normalized center coordinates of the crop window,
plus the normalized crop size (cw, ch) for a target aspect. The renderer interpolates between
keyframes. Centers are clamped so the crop never leaves the source frame.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class CropPath:
    aspect: float                 # target w/h
    cw: float                     # crop width as a fraction of the source width
    ch: float                     # crop height as a fraction of the source height
    keys: list[dict]              # [{"t": float, "cx": float, "cy": float}]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CropPath":
        return cls(aspect=d["aspect"], cw=d["cw"], ch=d["ch"], keys=list(d.get("keys", [])))

    def center_at(self, t: float) -> tuple[float, float]:
        if not self.keys:
            return 0.5, 0.5
        ts = [k["t"] for k in self.keys]
        cx = float(np.interp(t, ts, [k["cx"] for k in self.keys]))
        cy = float(np.interp(t, ts, [k["cy"] for k in self.keys]))
        return cx, cy

    def rect_at(self, t: float, src_w: int, src_h: int) -> tuple[int, int, int, int]:
        """Pixel crop rect (x, y, w, h) at time t."""
        cx, cy = self.center_at(t)
        w = int(round(self.cw * src_w))
        h = int(round(self.ch * src_h))
        x = int(round(cx * src_w - w / 2))
        y = int(round(cy * src_h - h / 2))
        x = max(0, min(src_w - w, x))
        y = max(0, min(src_h - h, y))
        return x, y, w, h


def crop_size(src_aspect: float, target_aspect: float, zoom: float = 1.0) -> tuple[float, float]:
    """Fractional crop (cw, ch) of the source that has the target aspect, scaled by 1/zoom."""
    if target_aspect < src_aspect:
        ch = 1.0
        cw = target_aspect / src_aspect
    else:
        cw = 1.0
        ch = src_aspect / target_aspect
    return min(1.0, cw / zoom), min(1.0, ch / zoom)


def build_crop_path(times: list[float], boxes: list[list[float] | None], in_t: float, out_t: float,
                    src_aspect: float, target_aspect: float, fallback_center: tuple[float, float] | None = None,
                    smooth_sigma_s: float = 0.35, sample_fps: float = 10.0, key_step: float = 0.2) -> CropPath:
    """Smooth the subject box centers inside [in_t, out_t] into a clamped crop path."""
    from scipy.ndimage import gaussian_filter1d

    cw, ch = crop_size(src_aspect, target_aspect)
    ts = np.asarray(times, dtype=np.float64)
    sel = (ts >= in_t - 0.3) & (ts <= out_t + 0.3)
    xs, ys, ok = [], [], []
    for t, b, keep in zip(times, boxes, sel):
        if not keep:
            continue
        if b is None:
            xs.append(np.nan)
            ys.append(np.nan)
        else:
            xs.append(b[0] + b[2] / 2)
            ys.append(b[1] + b[3] / 2)
        ok.append(t)
    fb = fallback_center or (0.5, 0.5)
    if not ok or np.all(np.isnan(xs)):
        return CropPath(target_aspect, cw, ch, [{"t": in_t, "cx": fb[0], "cy": fb[1]}, {"t": out_t, "cx": fb[0], "cy": fb[1]}])
    xs = np.asarray(xs)
    ys = np.asarray(ys)
    tt = np.asarray(ok)
    # fill gaps (no detected box) by interpolating between neighbors
    good = ~np.isnan(xs)
    xs = np.interp(tt, tt[good], xs[good])
    ys = np.interp(tt, tt[good], ys[good])
    sigma = max(0.5, smooth_sigma_s * sample_fps)
    if len(xs) > 2:
        xs = gaussian_filter1d(xs, sigma, mode="nearest")
        ys = gaussian_filter1d(ys, sigma, mode="nearest")
    # clamp so the crop stays inside the source
    xs = np.clip(xs, cw / 2, 1 - cw / 2)
    ys = np.clip(ys, ch / 2, 1 - ch / 2)
    keys = []
    t = in_t
    while t <= out_t + 1e-6:
        keys.append({"t": round(float(t), 3), "cx": round(float(np.interp(t, tt, xs)), 4),
                     "cy": round(float(np.interp(t, tt, ys)), 4)})
        t += key_step
    if keys[-1]["t"] < out_t:
        keys.append({"t": round(float(out_t), 3), "cx": round(float(np.interp(out_t, tt, xs)), 4),
                     "cy": round(float(np.interp(out_t, tt, ys)), 4)})
    return CropPath(target_aspect, cw, ch, keys)


def point_in_crop(path: CropPath, t: float, px: float, py: float, margin: float = 0.0) -> bool:
    """Whether a normalized source point lies inside the crop window at time t."""
    cx, cy = path.center_at(t)
    return (abs(px - cx) <= path.cw / 2 - margin) and (abs(py - cy) <= path.ch / 2 - margin)

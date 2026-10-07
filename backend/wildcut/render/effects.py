"""Per-frame effect evaluation. Everything is a pure function of (effect, timeline time, frame index)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np


def _seed(*parts) -> int:
    h = hashlib.md5(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:4], "little")


@dataclass
class GeomState:
    zoom: float = 1.0     # extra scale on top of the headroom zoom
    dx: float = 0.0       # shake translation, fraction of output width
    dy: float = 0.0
    rot: float = 0.0      # degrees


def active(effects: list[dict], t: float, fps: int = 30) -> list[dict]:
    """Effects active at timeline t, compared on frame indices so an effect never misses its frame."""
    k = int(round(t * fps))
    out = []
    for e in effects:
        if not e.get("enabled", True):
            continue
        ks = int(round(e["t"] * fps))
        ke = max(ks + 1, int(round((e["t"] + e["duration"]) * fps)))
        if ks <= k < ke:
            out.append(e)
    return out


def geometry(effects: list[dict], t: float, frame: int, fps: int) -> GeomState:
    g = GeomState()
    for e in effects:
        p = max(0.0, t - e["t"]) / max(e["duration"], 1e-6)   # 0..1 progress
        kind = e["type"]
        params = e.get("params", {})
        if kind == "shake":
            amp = float(params.get("amplitude", 0.02))
            rot = float(params.get("rotation_deg", 0.8))
            decay = (1 - p) ** 1.5
            rng = np.random.default_rng(_seed(e["id"], frame))
            g.dx += float(rng.uniform(-1, 1)) * amp * decay
            g.dy += float(rng.uniform(-1, 1)) * amp * decay * 0.8
            g.rot += float(rng.uniform(-1, 1)) * rot * decay
        elif kind == "zoom_punch":
            scale = float(params.get("scale", 1.08))
            g.zoom *= 1.0 + (scale - 1.0) * (1 - p) ** 2   # snap in, ease back
        elif kind == "push_in":
            scale = float(params.get("scale", 1.06))
            g.zoom *= 1.0 + (scale - 1.0) * p              # slow push across the clip
    return g


def flash_opacity(effects: list[dict], t: float) -> float:
    op = 0.0
    for e in effects:
        if e["type"] == "flash":
            p = max(0.0, t - e["t"]) / max(e["duration"], 1e-6)
            op = max(op, max(0.0, 1 - p) ** 1.2)
    return min(1.0, op)


def fade_black_opacity(effects: list[dict], t: float) -> float:
    op = 0.0
    for e in effects:
        if e["type"] == "fade_black":
            p = min(1.0, max(0.0, t - e["t"]) / max(e["duration"], 1e-6))
            if e.get("params", {}).get("hard"):
                op = max(op, 1.0 if p >= 0.12 else p / 0.12)   # snap to black and hold
            else:
                op = max(op, 1 - abs(2 * p - 1))   # triangle: black at the middle of the dip
    return min(1.0, op)


def chromatic_px(effects: list[dict], t: float, width: int) -> int:
    px = 0
    for e in effects:
        if e["type"] == "chromatic":
            p = min(1.0, max(0.0, t - e["t"]) / max(e["duration"], 1e-6))
            px = max(px, int(round(float(e.get("params", {}).get("px", 4)) * (1 - p) * width / 1080)))
    return px


def apply_chromatic(frame: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return frame
    out = frame.copy()
    out[:, px:, 2] = frame[:, :-px, 2]      # red shifted right
    out[:, :-px, 0] = frame[:, px:, 0]      # blue shifted left
    return out


def apply_glitch(frame: np.ndarray, effects: list[dict], t: float, frame_idx: int) -> np.ndarray:
    for e in effects:
        if e["type"] != "glitch":
            continue
        rng = np.random.default_rng(_seed(e["id"], frame_idx))
        H, W = frame.shape[:2]
        out = frame.copy()
        for _ in range(int(rng.integers(3, 7))):
            y0 = int(rng.integers(0, H - 8))
            h = int(rng.integers(4, max(6, H // 12)))
            shift = int(rng.integers(-W // 12, W // 12))
            out[y0:y0 + h] = np.roll(frame[y0:y0 + h], shift, axis=1)
        return out
    return frame


def apply_flash(frame: np.ndarray, opacity: float) -> np.ndarray:
    if opacity <= 0.002:
        return frame
    return np.clip(frame.astype(np.float32) + 255.0 * opacity, 0, 255).astype(np.uint8)


def apply_dim(frame: np.ndarray, opacity: float) -> np.ndarray:
    if opacity <= 0.002:
        return frame
    return (frame.astype(np.float32) * (1 - opacity)).astype(np.uint8)


class Overlays:
    """Grain, vignette, letterbox. Masks are precomputed per output size."""

    def __init__(self, cfg: dict, width: int, height: int):
        self.grain = float(cfg.get("grain", 0.0))
        self.vignette = float(cfg.get("vignette", 0.0))
        self.letterbox = bool(cfg.get("letterbox", False))
        self.motion_blur = float(cfg.get("motion_blur", 0.0))
        self.w, self.h = width, height
        self.vmask = None
        if self.vignette > 0:
            y, x = np.mgrid[0:height, 0:width]
            nx = (x - width / 2) / (width / 2)
            ny = (y - height / 2) / (height / 2)
            r = np.sqrt(nx ** 2 + ny ** 2) / np.sqrt(2)
            mask = 1 - self.vignette * np.clip((r - 0.45) / 0.55, 0, 1) ** 1.6
            self.vmask = mask.astype(np.float32)[..., None]
        self.bar = int(round(height * 0.12 / 2)) * 2 if self.letterbox else 0

    def apply(self, frame: np.ndarray, frame_idx: int) -> np.ndarray:
        out = frame
        if self.vmask is not None:
            out = (out.astype(np.float32) * self.vmask + 0.5).astype(np.uint8)
        if self.grain > 0:
            rng = np.random.default_rng(_seed("grain", frame_idx))
            noise = rng.normal(0, 255 * self.grain * 0.5, (self.h // 2, self.w // 2)).astype(np.float32)
            import cv2

            noise = cv2.resize(noise, (self.w, self.h), interpolation=cv2.INTER_LINEAR)
            out = np.clip(out.astype(np.float32) + noise[..., None], 0, 255).astype(np.uint8)
        if self.bar:
            out = out.copy()
            out[: self.bar] = 0
            out[-self.bar:] = 0
        return out


def motion_blur_strength(effects: list[dict], t: float) -> float:
    st = 0.0
    for e in effects:
        if e["type"] == "motion_blur":
            p = min(1.0, max(0.0, t - e["t"]) / max(e["duration"], 1e-6))
            st = max(st, float(e.get("params", {}).get("strength", 1.0)) * p)
    return st


def apply_motion_blur(frame: np.ndarray, strength: float) -> np.ndarray:
    """Horizontal directional blur whose length grows with strength (the chase 'blur pass')."""
    if strength <= 0.02:
        return frame
    import cv2

    k = max(3, int(round(strength * frame.shape[1] * 0.08)) | 1)
    kernel = np.zeros((1, k), np.float32)
    kernel[0, :] = 1.0 / k
    return cv2.filter2D(frame, -1, kernel, borderType=cv2.BORDER_REFLECT_101)

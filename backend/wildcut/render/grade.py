"""Color grade: .cube 3D LUT (resampled once to a 128-level nearest table) + contrast/sat/lift/gamma."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np

from wildcut.config import get_settings

log = logging.getLogger(__name__)
LEVELS = 128


def _parse_cube(path: Path) -> tuple[int, np.ndarray]:
    size = 0
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("TITLE") or line.startswith("DOMAIN"):
            continue
        if line.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
            continue
        if line.startswith("LUT_1D_SIZE"):
            raise ValueError("1D LUTs are not supported; use a 3D .cube")
        parts = line.split()
        if len(parts) == 3:
            rows.append([float(p) for p in parts])
    if size == 0 or len(rows) != size ** 3:
        raise ValueError(f"malformed .cube {path}: size={size} rows={len(rows)}")
    # .cube order: R varies fastest, then G, then B -> array[b, g, r]
    lut = np.asarray(rows, dtype=np.float32).reshape(size, size, size, 3)
    return size, lut


@lru_cache(maxsize=8)
def load_lut_table(name: str) -> np.ndarray | None:
    """Returns a (LEVELS^3, 3) uint8 table indexed by (r>>1)<<14 | (g>>1)<<7 | (b>>1), or None."""
    from scipy.ndimage import map_coordinates

    path = Path(name)
    if not path.is_absolute():
        path = get_settings().assets_dir / "luts" / name
    if not path.exists():
        log.warning("LUT %s not found; grading without a LUT", name)
        return None
    size, lut = _parse_cube(path)
    r = (np.arange(LEVELS) + 0.5) / LEVELS * (size - 1)
    bb, gg, rr = np.meshgrid(r, r, r, indexing="ij")
    coords = np.stack([bb.ravel(), gg.ravel(), rr.ravel()])
    out = np.empty((LEVELS ** 3, 3), np.float32)
    for c in range(3):
        out[:, c] = map_coordinates(lut[..., c], coords, order=1, mode="nearest")
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def apply_lut(frame_bgr: np.ndarray, table: np.ndarray, strength: float = 1.0) -> np.ndarray:
    r = frame_bgr[..., 2] >> 1
    g = frame_bgr[..., 1] >> 1
    b = frame_bgr[..., 0] >> 1
    idx = (b.astype(np.int32) << 14) | (g.astype(np.int32) << 7) | r.astype(np.int32)
    rgb = table[idx]
    out = rgb[..., ::-1]
    if strength < 0.999:
        out = (frame_bgr.astype(np.float32) * (1 - strength) + out.astype(np.float32) * strength + 0.5).astype(np.uint8)
    return np.ascontiguousarray(out)


@lru_cache(maxsize=16)
def tone_curve(contrast: float, lift: float, gamma: float) -> np.ndarray:
    x = np.arange(256, dtype=np.float32) / 255.0
    x = (x - 0.45) * contrast + 0.45
    x = x + lift
    x = np.clip(x, 0, 1) ** (1.0 / gamma if gamma else 1.0)
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


class Grade:
    def __init__(self, cfg: dict):
        self.table = load_lut_table(cfg["lut"]) if cfg.get("lut") else None
        self.strength = float(cfg.get("lut_strength", 1.0))
        self.contrast = float(cfg.get("contrast", 1.0))
        self.saturation = float(cfg.get("saturation", 1.0))
        self.lift = float(cfg.get("lift", 0.0))
        self.gamma = float(cfg.get("gamma", 1.0))
        self.curve = tone_curve(self.contrast, self.lift, self.gamma)
        self.identity = (self.table is None and abs(self.contrast - 1) < 1e-6 and abs(self.saturation - 1) < 1e-6
                         and abs(self.lift) < 1e-6 and abs(self.gamma - 1) < 1e-6)

    def apply(self, frame_bgr: np.ndarray) -> np.ndarray:
        import cv2

        if self.identity:
            return frame_bgr
        out = frame_bgr
        if self.table is not None:
            out = apply_lut(out, self.table, self.strength)
        if abs(self.saturation - 1) > 1e-3:
            gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
            gray3 = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            out = cv2.addWeighted(out, self.saturation, gray3, 1 - self.saturation, 0)
        if abs(self.contrast - 1) > 1e-3 or abs(self.lift) > 1e-4 or abs(self.gamma - 1) > 1e-3:
            out = cv2.LUT(out, self.curve)
        return out

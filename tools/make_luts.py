"""Generate the bundled .cube LUTs deterministically (33^3) into assets/luts/.

phonk      crushed blacks, high contrast, slight desaturation, cool shadows / neutral highlights
cinematic  dark moody grade, lifted teal/green shadows, soft highlights, gentle desaturation
chase      warm golden grade, warm highlights, slightly deeper shadows
showdown   black-and-white with a contrast curve

Custom .cube files dropped into assets/luts/ are picked up by name from a preset's grade.lut.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "luts"
N = 33


def _grid() -> np.ndarray:
    r = np.linspace(0, 1, N)
    b, g, rr = np.meshgrid(r, r, r, indexing="ij")  # cube order: R fastest
    return np.stack([rr, g, b], axis=-1).reshape(-1, 3)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722])


def _saturate(rgb: np.ndarray, s: float) -> np.ndarray:
    y = _luma(rgb)[:, None]
    return y + (rgb - y) * s


def _contrast(rgb: np.ndarray, c: float, pivot: float = 0.45) -> np.ndarray:
    return (rgb - pivot) * c + pivot


def _scurve(rgb: np.ndarray, k: float) -> np.ndarray:
    return 1 / (1 + np.exp(-k * (rgb - 0.5))) if k else rgb


def _norm_s(rgb: np.ndarray, k: float) -> np.ndarray:
    lo, hi = _scurve(np.zeros(1), k)[0], _scurve(np.ones(1), k)[0]
    return (_scurve(rgb, k) - lo) / (hi - lo)


def _tint_shadows(rgb: np.ndarray, tint: np.ndarray, amount: float) -> np.ndarray:
    y = _luma(rgb)[:, None]
    w = np.clip(1 - y / 0.5, 0, 1)
    return rgb + w * tint[None, :] * amount


def _tint_highlights(rgb: np.ndarray, tint: np.ndarray, amount: float) -> np.ndarray:
    y = _luma(rgb)[:, None]
    w = np.clip((y - 0.5) / 0.5, 0, 1)
    return rgb + w * tint[None, :] * amount


def phonk(rgb: np.ndarray) -> np.ndarray:
    x = rgb.copy()
    x = _norm_s(x, 7.0)
    x = _contrast(x, 1.18, 0.42)
    x = x - 0.045                                           # crush blacks
    x = x / (1 - 0.045)
    x = _saturate(x, 0.82)
    x = _tint_shadows(x, np.array([-0.02, 0.0, 0.05]), 0.9)   # cool shadows
    return np.clip(x, 0, 1)


def cinematic(rgb: np.ndarray) -> np.ndarray:
    x = rgb.copy()
    x = _norm_s(x, 5.0)
    x = x ** 1.12                                           # darker mids
    x = _contrast(x, 1.06, 0.4)
    x = _saturate(x, 0.78)
    x = _tint_shadows(x, np.array([-0.03, 0.04, 0.05]), 1.0)  # lifted teal/green shadows
    x = _tint_highlights(x, np.array([0.03, 0.015, -0.02]), 0.5)  # warm soft highlights
    x = x * 0.96 + 0.01                                     # slightly lifted, softened blacks
    return np.clip(x, 0, 1)


def chase(rgb: np.ndarray) -> np.ndarray:
    x = rgb.copy()
    x = _norm_s(x, 5.5)
    x = _contrast(x, 1.08, 0.45)
    x = _saturate(x, 1.02)
    x = _tint_highlights(x, np.array([0.06, 0.035, -0.04]), 1.0)  # golden highlights
    x = _tint_shadows(x, np.array([0.02, 0.0, -0.03]), 0.6)       # warm shadows
    x = x - 0.02
    return np.clip(x, 0, 1)


def showdown(rgb: np.ndarray) -> np.ndarray:
    y = _luma(rgb)
    y = _norm_s(y, 6.5)
    y = _contrast(y, 1.15, 0.45)
    y = np.clip(y, 0, 1)
    return np.stack([y, y, y], axis=-1)


def write_cube(path: Path, fn, title: str) -> None:
    rgb = _grid()
    out = np.clip(fn(rgb), 0, 1)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {N}", "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in out]
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("phonk", phonk), ("cinematic", cinematic), ("chase", chase), ("showdown", showdown)):
        write_cube(OUT / f"{name}.cube", fn, f"wildcut {name}")
        print("wrote", OUT / f"{name}.cube")


if __name__ == "__main__":
    main()

"""Letterbox / pillarbox detection: black bars that stay black across the whole film are cropped."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from wildcut.media import extract_frames

BLACK = 24


def detect_letterbox(path: str | Path, duration: float, samples: int = 36, width: int = 480) -> dict:
    """Returns {"top", "bottom", "left", "right"} in source pixels (0 when there are no bars)."""
    from wildcut.media import probe

    info = probe(path)
    times = [duration * (i + 0.5) / samples for i in range(samples)]
    frames = extract_frames(path, times, width=width)
    if not frames:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    # a row/column is a bar only if it is dark in (almost) every sampled frame
    lum = np.stack([f.max(axis=2) for f in frames]).astype(np.float32)   # (n, h, w)
    row_max = np.percentile(lum.max(axis=2), 90, axis=0)                  # brightest pixel per row, robust across frames
    col_max = np.percentile(lum.max(axis=1), 90, axis=0)
    h, w = lum.shape[1], lum.shape[2]

    def run(arr: np.ndarray, reverse: bool) -> int:
        seq = arr[::-1] if reverse else arr
        n = 0
        for v in seq:
            if v < BLACK:
                n += 1
            else:
                break
        return n

    top, bottom = run(row_max, False), run(row_max, True)
    left, right = run(col_max, False), run(col_max, True)
    # ignore tiny bars (encoder garbage) and bars that would eat the picture
    sy, sx = info.height / h, info.width / w
    out = {"top": int(top * sy), "bottom": int(bottom * sy), "left": int(left * sx), "right": int(right * sx)}
    for k in out:
        if out[k] < 8:
            out[k] = 0
    if out["top"] + out["bottom"] > info.height * 0.6 or out["left"] + out["right"] > info.width * 0.6:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    # round to even so the crop keeps 4:2:0 alignment
    for k in out:
        out[k] -= out[k] % 2
    return out


def crop_rect(bars: dict, width: int, height: int) -> tuple[int, int, int, int]:
    x, y = bars.get("left", 0), bars.get("top", 0)
    w = width - x - bars.get("right", 0)
    h = height - y - bars.get("bottom", 0)
    w -= w % 2
    h -= h % 2
    return x, y, max(2, w), max(2, h)

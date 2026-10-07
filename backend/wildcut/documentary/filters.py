"""Cheap per-shot filters that run before any Claude call: black frames, burned-in text, duplicates."""
from __future__ import annotations

import numpy as np


def is_black(frames: list[np.ndarray], thresh: float = 14.0) -> bool:
    return bool(frames) and all(float(f.mean()) < thresh for f in frames)


def dhash(frame_bgr: np.ndarray, size: int = 8) -> int:
    """Difference hash: 64-bit perceptual fingerprint, robust to scaling and mild compression."""
    import cv2

    g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = g[:, 1:] > g[:, :-1]
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(b)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def text_score(frame_bgr: np.ndarray) -> float:
    """0..1 likelihood of burned-in text: rows of small, aligned, high-contrast components.

    Subtitles, lower thirds, title cards and credits all produce many letter-sized blobs that
    share a baseline. Wildlife footage rarely does.
    """
    import cv2

    h, w = frame_bgr.shape[:2]
    scale = 480 / w
    small = cv2.resize(frame_bgr, (480, int(h * scale)), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sh, sw = g.shape
    best = 0.0
    for polarity in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        # local contrast: text is much brighter (or darker) than its surroundings
        blur = cv2.GaussianBlur(g, (0, 0), 6)
        diff = cv2.subtract(g, blur) if polarity == cv2.THRESH_BINARY else cv2.subtract(blur, g)
        _, mask = cv2.threshold(diff, 40, 255, cv2.THRESH_BINARY)
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        letters = []
        for i in range(1, n):
            x, y, cw, ch, area = stats[i]
            if 0.012 * sh <= ch <= 0.09 * sh and 0.15 <= cw / max(1, ch) <= 2.2 and area >= 0.25 * cw * ch:
                letters.append((x, y, cw, ch))
        if len(letters) < 6:
            continue
        # group by baseline (bottom y) and look for a run of >= 6 letters of similar height
        letters.sort(key=lambda l: l[1] + l[3])
        rows: list[list[tuple]] = []
        for l in letters:
            base = l[1] + l[3]
            if rows and abs((rows[-1][-1][1] + rows[-1][-1][3]) - base) <= max(3, 0.4 * l[3]):
                rows[-1].append(l)
            else:
                rows.append([l])
        for row in rows:
            if len(row) < 8:
                continue
            row.sort(key=lambda l: l[0])
            hs = np.array([l[3] for l in row], dtype=np.float32)
            tops = np.array([l[1] for l in row], dtype=np.float32)
            # glyphs on one line share a height and a top edge; random blobs do not
            if hs.std() / max(1.0, hs.mean()) > 0.3 or tops.std() > 0.45 * hs.mean():
                continue
            xs = np.array([l[0] for l in row], dtype=np.float32)
            gaps = np.diff(xs)
            if len(gaps) < 7:
                continue
            med = float(np.median(gaps))
            if not (0.3 * hs.mean() <= med <= 2.5 * hs.mean()):
                continue
            if gaps.std() / max(1.0, med) > 1.2:
                continue
            best = max(best, min(1.0, len(row) / 14.0))
    return best


def has_text(frames: list[np.ndarray], thresh: float = 0.5) -> bool:
    return any(text_score(f) >= thresh for f in frames)


def static_corner_logo(frames: list[np.ndarray]) -> bool:
    """A corner region that is identical and edge-rich in every sampled frame (watermark / channel bug)."""
    import cv2

    if len(frames) < 2:
        return False
    h, w = frames[0].shape[:2]
    ch, cw = int(h * 0.14), int(w * 0.18)
    corners = [(slice(0, ch), slice(0, cw)), (slice(0, ch), slice(w - cw, w)), (slice(h - ch, h), slice(0, cw)), (slice(h - ch, h), slice(w - cw, w))]
    for ys, xs in corners:
        patches = [cv2.cvtColor(f[ys, xs], cv2.COLOR_BGR2GRAY) for f in frames]
        edges = cv2.Canny(patches[0], 80, 160)
        if edges.mean() < 6:
            continue
        diffs = [np.abs(p.astype(int) - patches[0].astype(int)).mean() for p in patches[1:]]
        if max(diffs) < 3.0:
            return True
    return False

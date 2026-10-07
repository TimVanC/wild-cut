"""Synthetic "documentary" for Documentary mode tests (10+ minutes, labeled segments).

The film is letterboxed (black bars top and bottom). Segment kinds and what they stand for:
  action      the target animal (white disc) with motion bursts        -> HERO
  closeup     a large slow-turning disc filling the frame               -> AURA
  landscape   textured world with a slow pan and no animal              -> BROLL
  other       a different animal (orange square) moving                 -> OTHER
  text        an action/landscape shot with burned-in text / lower third -> filtered (text)
  presenter   a "person": skin-tone head + dark body, talking           -> filtered (people)
  black       black frames                                               -> filtered (black)
  short       a 0.3 s flash shot                                         -> filtered (<0.5 s)
  repeat      byte-identical repeat of an earlier action segment        -> filtered (duplicate)

ground_truth_documentary.json lists every segment with start/end/kind (and burst peak times
for action segments) plus the letterbox geometry and the long track's beat grid.

Usage: python tools/make_documentary_asset.py [out_dir] (default tests_out/)
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_test_assets import _ffmpeg_writer, _texture, _velocity, make_track  # noqa: E402

W, H, FPS = 960, 540, 30
BAR = 68                  # letterbox bar height (content is 960x404)
DOC_VERSION = 2


def _font(size: int):
    for cand in ("C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(cand).exists():
            return ImageFont.truetype(cand, size)
    return ImageFont.load_default()


class Film:
    def __init__(self, path: Path, seed: int = 11):
        self.proc = _ffmpeg_writer(path)
        self.rng = np.random.default_rng(seed)
        self.t = 0.0
        self.segments: list[dict] = []
        self.cache: dict[str, np.ndarray] = {}
        self.font = _font(34)
        self.small = _font(22)

    def bg(self, key: str, base, w: int = W + 400) -> np.ndarray:
        if key not in self.cache:
            self.cache[key] = _texture(np.random.default_rng(abs(hash(key)) % 100000), w, H - 2 * BAR, base)
        return self.cache[key]

    def emit(self, frame: np.ndarray) -> None:
        full = np.zeros((H, W, 3), np.uint8)
        full[BAR:H - BAR] = frame[:H - 2 * BAR, :W]
        self.proc.stdin.write(full.tobytes())

    def seg(self, kind: str, duration: float, **extra) -> dict:
        d = {"kind": kind, "start": round(self.t, 3), "end": round(self.t + duration, 3), **extra}
        self.segments.append(d)
        self.t += duration
        return d

    # ---- segment generators (each writes exactly round(duration*FPS) frames)
    def action(self, duration: float, bursts: list[tuple[float, float]], key: str, base, start_x: float = 150.0, drift: float = 25.0):
        import cv2

        n = int(round(duration * FPS))
        bg = self.bg(key, base)
        x, ch = start_x, H - 2 * BAR
        for i in range(n):
            t = i / FPS
            x += _velocity(t, bursts, drift) / FPS
            frame = bg[:, :W].copy()
            y = int(ch / 2 + 20 * math.sin(t * 1.1))
            sx = int(x) % (W + 80) - 40
            cv2.circle(frame, (sx, y), 36, (235, 235, 245), -1)
            cv2.circle(frame, (sx, y), 36, (30, 30, 40), 3)
            cv2.circle(frame, (sx + 11, y - 9), 7, (40, 40, 60), -1)
            self.emit(frame)
        return self.seg("action", duration, peaks=[b for b, _ in bursts], bg=key)

    def closeup(self, duration: float, key: str, base):
        import cv2

        n = int(round(duration * FPS))
        bg = self.bg(key, base)
        ch = H - 2 * BAR
        for i in range(n):
            t = i / FPS
            frame = bg[:, :W].copy()
            frame = (frame * 0.45).astype(np.uint8)          # darker, moody
            cx, cy = int(W / 2 + 12 * math.sin(t * 0.5)), int(ch / 2)
            cv2.circle(frame, (cx, cy), 150, (238, 236, 240), -1)
            cv2.circle(frame, (cx, cy), 150, (28, 28, 36), 6)
            ex = int(cx + 45 * math.cos(t * 0.6))
            cv2.circle(frame, (ex, cy - 35), 24, (36, 36, 52), -1)
            self.emit(frame)
        return self.seg("closeup", duration, bg=key)

    def landscape(self, duration: float, key: str, base, pan: float = 30.0):
        n = int(round(duration * FPS))
        bg = self.bg(key, base, w=W + int(pan * duration) + 64)
        for i in range(n):
            cam = int(pan * i / FPS)
            self.emit(bg[:, cam:cam + W].copy())
        return self.seg("landscape", duration, bg=key)

    def other(self, duration: float, key: str, base):
        import cv2

        n = int(round(duration * FPS))
        bg = self.bg(key, base)
        ch = H - 2 * BAR
        for i in range(n):
            t = i / FPS
            frame = bg[:, :W].copy()
            x = int(100 + (W - 200) * (0.5 + 0.5 * math.sin(t * 1.4)))
            y = int(ch / 2 + 60 * math.cos(t * 2.1))
            cv2.rectangle(frame, (x - 40, y - 30), (x + 40, y + 30), (40, 140, 255), -1)
            cv2.rectangle(frame, (x - 40, y - 30), (x + 40, y + 30), (20, 40, 90), 3)
            self.emit(frame)
        return self.seg("other", duration, bg=key)

    def text_over(self, duration: float, key: str, base, line: str, lower_third: bool = True):
        import cv2

        n = int(round(duration * FPS))
        bg = self.bg(key, base)
        ch = H - 2 * BAR
        for i in range(n):
            t = i / FPS
            frame = bg[:, :W].copy()
            sx = int(200 + 40 * t)
            cv2.circle(frame, (sx, ch // 2), 36, (235, 235, 245), -1)
            img = Image.fromarray(frame[..., ::-1])
            d = ImageDraw.Draw(img)
            if lower_third:
                d.rectangle((40, ch - 95, 620, ch - 35), fill=(12, 12, 14))
                d.text((56, ch - 86), line, font=self.font, fill=(255, 255, 255))
            else:
                d.text((W / 2, ch - 50), line, font=self.small, fill=(255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))
            self.emit(np.asarray(img)[..., ::-1].copy())
        return self.seg("text", duration, bg=key)

    def presenter(self, duration: float, key: str, base):
        import cv2

        n = int(round(duration * FPS))
        bg = self.bg(key, base)
        ch = H - 2 * BAR
        for i in range(n):
            t = i / FPS
            frame = (self.bg(key, base)[:, :W] * 0.7).astype(np.uint8)
            cx = W // 2
            cv2.rectangle(frame, (cx - 120, 250), (cx + 120, ch), (60, 40, 30), -1)        # dark jacket
            cv2.ellipse(frame, (cx, 200), (70, 90), 0, 0, 360, (150, 190, 235), -1)        # skin tone (BGR)
            cv2.circle(frame, (cx - 25, 185), 8, (30, 30, 30), -1)
            cv2.circle(frame, (cx + 25, 185), 8, (30, 30, 30), -1)
            mouth = 6 + int(6 * abs(math.sin(t * 9)))
            cv2.ellipse(frame, (cx, 240), (22, mouth), 0, 0, 360, (40, 30, 60), -1)
            self.emit(frame)
        return self.seg("presenter", duration, bg=key)

    def black(self, duration: float):
        n = int(round(duration * FPS))
        for _ in range(n):
            self.emit(np.zeros((H - 2 * BAR, W, 3), np.uint8))
        return self.seg("black", duration)

    def short(self, duration: float = 0.3):
        import cv2

        n = max(1, int(round(duration * FPS)))
        for i in range(n):
            frame = np.full((H - 2 * BAR, W, 3), 200, np.uint8)
            cv2.circle(frame, (W // 2, (H - 2 * BAR) // 2), 60, (0, 0, 255), -1)
            self.emit(frame)
        return self.seg("short", duration)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()
        if self.proc.returncode != 0:
            raise RuntimeError("ffmpeg failed")


def make_documentary(out_dir: Path) -> dict:
    path = out_dir / "documentary.mp4"
    film = Film(path)
    rng = np.random.default_rng(3)
    palettes = [(70, 120, 60), (60, 110, 150), (140, 100, 50), (90, 70, 120), (60, 130, 130), (150, 140, 60)]
    actions: list[dict] = []
    k = 0
    # ~10.5 minutes: alternate blocks of landscape / closeup / action (x3) / other / filtered material
    for block in range(12):
        pal = palettes[block % len(palettes)]
        film.landscape(6.0, f"land{block}", pal, pan=25.0 + 5 * (block % 3))
        if block % 4 == 0:
            film.black(1.0)
        film.closeup(5.0, f"close{block}", pal)
        for j in range(3):
            d = 9.0 + (j % 2) * 2.0
            amps = [700.0 + 120 * ((block + j) % 4), 500.0 + 90 * (j % 3)]
            bursts = [(2.0 + 0.5 * j, amps[0]), (d - 3.0, amps[1])]
            seg = film.action(d, bursts, f"act{block}_{j}", pal, start_x=120.0 + 40 * j)
            seg["index"] = k
            actions.append(seg)
            k += 1
        if block % 3 == 1:
            film.other(6.0, f"other{block}", pal)
        if block % 3 == 2:
            film.text_over(6.0, f"text{block}", pal, "NARRATOR  THE HUNT BEGINS AT DAWN", lower_third=(block % 2 == 0))
        if block % 4 == 3:
            film.presenter(6.0, f"pres{block}", pal)
        if block % 5 == 2:
            film.short(0.3)
        if block in (5, 9):
            # repeat an earlier action segment exactly (same key, same bursts, same start) -> duplicate
            src = actions[block - 4]
            dur = src["end"] - src["start"]
            seg = film.action(dur, [(p, 700.0) for p in src["peaks"]], src["bg"], palettes[(block - 4) % len(palettes)],
                              start_x=120.0 + 40 * ((block - 4) % 3))
            seg["kind"] = "repeat"
            seg["repeat_of"] = src["start"]
    film.close()
    gt = {"version": DOC_VERSION, "path": path.name, "duration": round(film.t, 3), "fps": FPS, "width": W, "height": H,
          "letterbox": {"top": BAR, "bottom": BAR, "left": 0, "right": 0}, "segments": film.segments,
          "counts": {kind: sum(1 for s in film.segments if s["kind"] == kind) for kind in
                     ("action", "closeup", "landscape", "other", "text", "presenter", "black", "short", "repeat")}}
    track = make_track(out_dir / "phonk_long.wav", bpm=140.0, duration=90.0, drop_time=40.0)
    gt["track"] = track
    (out_dir / "ground_truth_documentary.json").write_text(json.dumps(gt, indent=1))
    print(f"documentary: {film.t:.1f}s, {len(film.segments)} segments -> {path}")
    return gt


if __name__ == "__main__":
    make_documentary(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "tests_out")

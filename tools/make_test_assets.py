"""Generate synthetic ground-truth test assets for Wild Cut.

Clips (960x540 @ 30 fps, H.264):
  static_peaks.mp4   static textured background; a disc that accelerates sharply at known times.
  panning.mp4        camera pans steadily over a textured world (global motion); the disc moves
                     with the world except for one burst. Baseline subject motion must stay low.
  two_shots.mp4      a hard cut at a known time between two visually distinct shots.
  clip_a/b/c.mp4     three single-shot clips with one burst each at different strengths (planner tests).

Audio:
  phonk_test.wav     140 BPM, 30 s. Quiet build for 12 s (kick + hats at low gain, riser),
                     drop at exactly 12.0 s (full-gain kick, 808 bass pattern, cowbell).

ground_truth.json records every known time so tests assert against real numbers.

Usage: python tools/make_test_assets.py [out_dir]   (default: tests_out/)
Everything is deterministic (fixed seeds); re-running produces identical files byte-for-byte
except for container timestamps, which ffmpeg is told to zero.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 960, 540, 30
SR = 44100
ASSET_VERSION = 3  # bump when the generator changes so cached assets regenerate


def _ffmpeg_writer(path: Path, fps: int = FPS, size: tuple[int, int] = (W, H)) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact",
        str(path),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def _texture(rng: np.random.Generator, w: int, h: int, base: tuple[int, int, int]) -> np.ndarray:
    """Textured background so optical flow has features to lock onto (mimics grass/rock)."""
    noise = rng.normal(0, 1, (h // 4, w // 4)).astype(np.float32)
    import cv2

    noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_CUBIC)
    noise = cv2.GaussianBlur(noise, (0, 0), 1.5)
    noise = (noise - noise.min()) / (noise.max() - noise.min() + 1e-6)
    img = np.zeros((h, w, 3), np.uint8)
    for c in range(3):
        img[..., c] = np.clip(base[c] * (0.55 + 0.9 * noise), 0, 255).astype(np.uint8)
    # a few high-contrast blotches
    for _ in range(60):
        cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(6, 28))
        col = tuple(int(x) for x in rng.integers(20, 235, 3))
        cv2.circle(img, (cx, cy), r, col, -1)
    return img


def _velocity(t: float, bursts: list[tuple[float, float]], base: float) -> float:
    """Subject x-velocity (px/s): slow drift plus gaussian bursts centered on the peak times."""
    v = base
    for center, amp in bursts:
        v += amp * math.exp(-0.5 * ((t - center) / 0.12) ** 2)
    return v


def make_clip(path: Path, duration: float, bursts: list[tuple[float, float]], bg_base: tuple[int, int, int],
              pan_speed: float = 0.0, seed: int = 1, start_x: float = 120.0, base_drift: float = 20.0,
              cut_at: float | None = None, bg_base2: tuple[int, int, int] | None = None) -> dict:
    import cv2

    rng = np.random.default_rng(seed)
    n_frames = int(round(duration * FPS))
    world_w = W + int(abs(pan_speed) * duration) + 64
    bg = _texture(rng, world_w, H, bg_base)
    bg2 = _texture(np.random.default_rng(seed + 100), world_w, H, bg_base2) if bg_base2 else None

    # integrate velocity at fine resolution for subject x (world coordinates)
    dt = 1.0 / FPS
    x = start_x
    y0 = H / 2
    proc = _ffmpeg_writer(path)
    assert proc.stdin is not None
    peak_positions = {}
    for i in range(n_frames):
        t = i * dt
        v = _velocity(t, bursts, base_drift)
        x += v * dt
        cam = pan_speed * t
        src = bg2 if (cut_at is not None and t >= cut_at and bg2 is not None) else bg
        frame = src[:, int(cam):int(cam) + W].copy()
        y = y0 + 18 * math.sin(t * 1.3)
        sx = int(round(x - cam)) if pan_speed == 0 else int(round(x))  # when panning the disc is in-world
        if pan_speed != 0:
            sx = int(round(x - cam))
        cv2.circle(frame, (sx, int(y)), 38, (235, 235, 245), -1)
        cv2.circle(frame, (sx, int(y)), 38, (30, 30, 40), 3)
        cv2.circle(frame, (sx + 12, int(y) - 10), 8, (40, 40, 60), -1)  # an "eye" so it reads as a creature
        for center, _ in bursts:
            if abs(t - center) < dt / 2:
                peak_positions[f"{center:.2f}"] = {"x": sx / W, "y": y / H}
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {path}")
    return {
        "path": path.name,
        "duration": duration,
        "fps": FPS,
        "peaks": [c for c, _ in bursts],
        "peak_amplitudes": [a for _, a in bursts],
        "peak_positions": peak_positions,
        "pan_speed": pan_speed,
        "cut_at": cut_at,
    }


# ---------------------------------------------------------------- audio

def _env(n: int, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.exp(-t / decay)


def _kick(gain: float) -> np.ndarray:
    n = int(0.18 * SR)
    t = np.arange(n) / SR
    f = 150 * np.exp(-t / 0.035) + 48
    phase = 2 * np.pi * np.cumsum(f) / SR
    return gain * np.sin(phase) * _env(n, 0.07)


def _bass808(gain: float, freq: float = 55.0, length: float = 0.42) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    body = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2 * t)
    body = np.tanh(2.2 * body)  # saturation, like a distorted 808
    return gain * body * _env(n, 0.16) * np.minimum(1.0, t / 0.004)


def _hat(gain: float, rng: np.random.Generator) -> np.ndarray:
    n = int(0.035 * SR)
    noise = rng.normal(0, 1, n)
    # crude high-pass: difference
    noise = np.diff(noise, prepend=0.0)
    return gain * noise * _env(n, 0.012)


def _cowbell(gain: float) -> np.ndarray:
    n = int(0.12 * SR)
    t = np.arange(n) / SR
    s = np.sign(np.sin(2 * np.pi * 562 * t)) * 0.6 + np.sign(np.sin(2 * np.pi * 845 * t)) * 0.4
    return gain * s * _env(n, 0.04)


def _add(buf: np.ndarray, start_s: float, sig: np.ndarray) -> None:
    i = int(round(start_s * SR))
    j = min(len(buf), i + len(sig))
    if j > i:
        buf[i:j] += sig[: j - i]


def make_track(path: Path, bpm: float = 140.0, duration: float = 30.0, drop_time: float = 12.0) -> dict:
    import soundfile as sf

    rng = np.random.default_rng(7)
    beat = 60.0 / bpm
    n = int(duration * SR)
    buf = np.zeros(n, np.float64)
    beats = []
    downbeats = []
    bass_hits = []
    kicks = []
    # 808 pattern in 8th notes per bar (8 slots): hits on slots 0, 3, 5 (classic syncopated phonk)
    bass_slots = [0, 3, 5]
    k = 0
    t = 0.0
    while t < duration - 1e-9:
        beats.append(round(t, 6))
        bar_pos = k % 4
        if bar_pos == 0:
            downbeats.append(round(t, 6))
        pre = t < drop_time - 1e-6
        # kick on every beat (quiet in the build)
        _add(buf, t, _kick(0.22 if pre else 0.95))
        kicks.append(round(t, 6))
        # hats on 8ths
        _add(buf, t, _hat(0.05 if pre else 0.12, rng))
        _add(buf, t + beat / 2, _hat(0.04 if pre else 0.10, rng))
        if not pre:
            # cowbell on beats 2 and 4
            if bar_pos in (1, 3):
                _add(buf, t, _cowbell(0.25))
            # 808 pattern
            for slot in bass_slots:
                if slot // 2 == bar_pos:
                    hit_t = t + (slot % 2) * beat / 2
                    freq = 55.0 if slot != 5 else 41.2
                    _add(buf, hit_t, _bass808(0.9, freq))
                    bass_hits.append(round(hit_t, 6))
        t += beat
        k += 1
    # riser into the drop: band-limited noise sweep rising in level over the last 4 s of the build
    rise_len = 4.0
    rs = int((drop_time - rise_len) * SR)
    rn = int(rise_len * SR)
    noise = rng.normal(0, 1, rn)
    # moving-average lowpass that opens up over time
    ramp = np.linspace(0, 1, rn)
    riser = noise * (0.02 + 0.10 * ramp**2)
    buf[rs:rs + rn] += riser
    # snare roll doubling in density toward the drop
    tt = drop_time - rise_len
    step = beat
    while tt < drop_time - 1e-6:
        _add(buf, tt, _hat(0.09, rng))
        tt += step
        if tt > drop_time - 2.0:
            step = beat / 2
        if tt > drop_time - 1.0:
            step = beat / 4
    # a soft pad under the build so it is not silence
    tpad = np.arange(n) / SR
    pad = 0.03 * np.sin(2 * np.pi * 110 * tpad) * (tpad < drop_time)
    buf += pad
    peak = np.max(np.abs(buf))
    buf = 0.98 * buf / peak
    sf.write(str(path), buf.astype(np.float32), SR, subtype="PCM_16")
    return {
        "path": path.name,
        "bpm": bpm,
        "beat_period": beat,
        "duration": duration,
        "beats": beats,
        "downbeats": downbeats,
        "kicks": kicks,
        "bass_hits": bass_hits,
        "drop_time": drop_time,
    }


def main(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    gt: dict = {"version": ASSET_VERSION, "clips": {}, "track": None}
    gt["clips"]["static_peaks"] = make_clip(
        out_dir / "static_peaks.mp4", 12.0, [(3.0, 700.0), (7.5, 800.0)], (70, 120, 60), seed=1)
    gt["clips"]["panning"] = make_clip(
        out_dir / "panning.mp4", 10.0, [(6.0, 750.0)], (90, 110, 150), pan_speed=160.0, seed=2, start_x=160.0)
    gt["clips"]["two_shots"] = make_clip(
        out_dir / "two_shots.mp4", 8.0, [(2.0, 500.0), (6.0, 600.0)], (140, 90, 50), seed=3,
        cut_at=4.0, bg_base2=(50, 80, 160))
    gt["clips"]["clip_a"] = make_clip(out_dir / "clip_a.mp4", 6.0, [(2.0, 950.0)], (60, 130, 80), seed=4)
    gt["clips"]["clip_b"] = make_clip(out_dir / "clip_b.mp4", 6.0, [(3.5, 500.0)], (120, 70, 140), seed=5)
    gt["clips"]["clip_c"] = make_clip(out_dir / "clip_c.mp4", 6.0, [(1.5, 650.0)], (150, 140, 60), seed=6)
    gt["track"] = make_track(out_dir / "phonk_test.wav")
    (out_dir / "ground_truth.json").write_text(json.dumps(gt, indent=2))
    print(f"wrote synthetic assets to {out_dir}")
    return gt


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests_out")

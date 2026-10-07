"""Music analysis: tempo, beats, downbeats, bass hits (<150 Hz onsets), drop candidates.

All times are seconds from the start of the audio file. The chosen song window is applied by
the planner; the grid always covers the whole file so the user can move the window freely.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

SR = 22050
HOP = 256
LOW_CUTOFF_HZ = 150.0


@dataclass
class DropCandidate:
    t: float
    score: float
    energy_jump: float
    onset_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BeatGridData:
    tempo: float
    duration: float
    beats: list[float]
    downbeats: list[float]
    bass_hits: list[float]
    drop_candidates: list[dict]
    chosen_drop: float | None
    waveform: list[float] = field(default_factory=list)   # 0..1 envelope for the UI, ~1500 points
    low_energy: list[float] = field(default_factory=list)  # per-second low-band RMS for the UI/debug

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BeatGridData":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    # --- helpers used by the planner and the Director chat
    def beats_in(self, start: float, end: float) -> list[float]:
        return [b for b in self.beats if start - 1e-6 <= b <= end + 1e-6]

    def downbeats_in(self, start: float, end: float) -> list[float]:
        return [b for b in self.downbeats if start - 1e-6 <= b <= end + 1e-6]

    def bass_hits_in(self, start: float, end: float) -> list[float]:
        return [b for b in self.bass_hits if start - 1e-6 <= b <= end + 1e-6]

    def nearest_beat(self, t: float) -> float:
        if not self.beats:
            return t
        arr = np.asarray(self.beats)
        return float(arr[int(np.argmin(np.abs(arr - t)))])

    def beat_period(self) -> float:
        if self.tempo > 0:
            return 60.0 / self.tempo
        if len(self.beats) > 1:
            return float(np.median(np.diff(self.beats)))
        return 0.5


def load_audio(path: str | Path, sr: int = SR) -> tuple[np.ndarray, int]:
    import librosa

    y, sr_out = librosa.load(str(path), sr=sr, mono=True)
    return y.astype(np.float32), int(sr_out)


def _lowpass(y: np.ndarray, sr: int, cutoff: float = LOW_CUTOFF_HZ) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, cutoff / (sr / 2), btype="low", output="sos")
    return sosfiltfilt(sos, y).astype(np.float32)


def _frame_rms(y: np.ndarray, frame: int = 2048, hop: int = HOP) -> np.ndarray:
    import librosa

    return librosa.feature.rms(y=y, frame_length=frame, hop_length=hop, center=True)[0]


def low_band_onsets(y: np.ndarray, sr: int, rel_height: float = 0.25, min_gap_s: float = 0.15) -> tuple[list[float], list[float]]:
    """Sharp energy rises in the <150 Hz band (kicks and 808s). Returns (times, strengths 0..1)."""
    import librosa
    from scipy.ndimage import uniform_filter1d
    from scipy.signal import find_peaks

    hop = 64
    low = _lowpass(y, sr)
    rms = librosa.feature.rms(y=low, frame_length=512, hop_length=hop, center=True)[0]
    if rms.max() <= 0:
        return [], []
    lg = np.log1p(50 * rms / rms.max())
    d = np.diff(lg, prepend=lg[0])
    d[d < 0] = 0
    d = uniform_filter1d(d, 3)
    if d.max() <= 0:
        return [], []
    idx, props = find_peaks(d, height=rel_height * d.max(), distance=max(1, int(min_gap_s * sr / hop)))
    times = [round(float(i * hop / sr), 4) for i in idx]
    strengths = [round(float(h / d.max()), 4) for h in props["peak_heights"]]
    return times, strengths


def _regularize_beats(beats: list[float], duration: float) -> list[float]:
    """Fill gaps and extrapolate to the file edges using the median beat period."""
    if len(beats) < 3:
        return beats
    period = float(np.median(np.diff(beats)))
    out = [beats[0]]
    for b in beats[1:]:
        gap = b - out[-1]
        n_missing = int(round(gap / period)) - 1
        if 1 <= n_missing <= 4:
            for k in range(1, n_missing + 1):
                out.append(out[-1] + (gap / (n_missing + 1)))
        out.append(b)
    while out[0] - period >= -0.03:
        out.insert(0, max(0.0, out[0] - period))
    while out[-1] + period <= duration - 0.03:
        out.append(out[-1] + period)
    return [round(float(b), 4) for b in out]


def detect_beats(y: np.ndarray, sr: int) -> tuple[float, list[float]]:
    import librosa

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, aggregate=np.median)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP, trim=False, units="frames")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = [float(b) for b in librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)]
    # snap each beat onto a sharp low-band onset (kick) within +-45 ms: sample-accurate, no spectrogram lag
    lows, _ = low_band_onsets(y, sr, rel_height=0.12)
    if lows:
        arr = np.asarray(lows)
        snapped = []
        for b in beats:
            k = int(np.argmin(np.abs(arr - b)))
            snapped.append(float(arr[k]) if abs(arr[k] - b) <= 0.045 else b)
        beats = snapped
    beats = _regularize_beats(sorted(set(round(b, 4) for b in beats)), len(y) / sr)
    if len(beats) > 2:
        tempo = 60.0 / float(np.median(np.diff(beats)))
    return tempo, beats


def detect_bass_hits(y: np.ndarray, sr: int, beats: list[float]) -> list[float]:
    """Strong onsets in the <150 Hz band (808s and hard kicks)."""
    times, _ = low_band_onsets(y, sr, rel_height=0.25)
    return times


def pick_downbeats(beats: list[float], y: np.ndarray, sr: int, drop: float | None = None) -> list[float]:
    """Phase (0..3) whose beats carry the most low-band energy; a known drop must be a downbeat."""
    import librosa

    if len(beats) < 4:
        return list(beats)
    low = _lowpass(y, sr)
    rms = _frame_rms(low)
    best_phase, best_score = 0, -1.0
    for phase in range(4):
        score = 0.0
        for i in range(phase, len(beats), 4):
            f = int(round(beats[i] * sr / HOP))
            score += float(rms[min(len(rms) - 1, f): min(len(rms), f + 6)].max()) if f < len(rms) else 0.0
        if score > best_score:
            best_phase, best_score = phase, score
    if drop is not None:
        arr = np.asarray(beats)
        k = int(np.argmin(np.abs(arr - drop)))
        if abs(arr[k] - drop) < 0.08:
            best_phase = k % 4
    return [beats[i] for i in range(best_phase, len(beats), 4)]


def detect_drop(y: np.ndarray, sr: int, beats: list[float], bass_hits: list[float], max_candidates: int = 3) -> list[DropCandidate]:
    """Largest jump in low-band energy after a quieter build, confirmed by onset density."""
    low = _lowpass(y, sr)
    rms = _frame_rms(low)
    fps = sr / HOP
    step = max(1, int(round(0.1 * fps)))   # evaluate every 100 ms
    before_w, after_w = int(4.0 * fps), int(2.0 * fps)
    n = len(rms)
    cands: list[tuple[float, float, float, float]] = []
    hits = np.asarray(bass_hits) if bass_hits else np.zeros(0)
    for f in range(int(2.0 * fps), n - after_w, step):
        before = rms[max(0, f - before_w):f]
        after = rms[f:f + after_w]
        if len(before) < 4 or len(after) < 4:
            continue
        b = float(np.mean(before)) + 1e-6
        a = float(np.mean(after)) + 1e-6
        jump = (a - b) / (a + b)      # -1..1, >0 when energy rises
        if jump <= 0.05:
            continue
        t = f / fps
        if len(hits):
            dens_after = float(np.sum((hits >= t) & (hits < t + 4.0)))
            dens_before = float(np.sum((hits >= t - 4.0) & (hits < t)))
            onset_ratio = (dens_after + 0.5) / (dens_before + 0.5)
        else:
            onset_ratio = 1.0
        score = jump * a * (1.0 + 0.25 * np.log1p(onset_ratio))
        cands.append((t, score, jump, onset_ratio))
    if not cands:
        return []
    cands.sort(key=lambda c: c[1], reverse=True)
    picked: list[DropCandidate] = []
    for t, score, jump, ratio in cands:
        if any(abs(t - p.t) < 4.0 for p in picked):
            continue
        # refine onto the local max of the energy derivative, then snap to the nearest beat
        picked.append(DropCandidate(t=_snap(t, beats, max_dist=0.25), score=float(score), energy_jump=float(jump), onset_ratio=float(ratio)))
        if len(picked) >= max_candidates:
            break
    top = picked[0].score or 1.0
    for p in picked:
        p.score = round(p.score / top, 4)
        p.t = round(p.t, 4)
    return picked


def _snap(t: float, beats: list[float], max_dist: float) -> float:
    if not beats:
        return t
    arr = np.asarray(beats)
    k = int(np.argmin(np.abs(arr - t)))
    return float(arr[k]) if abs(arr[k] - t) <= max_dist else t


def waveform_envelope(y: np.ndarray, points: int = 1500) -> list[float]:
    if len(y) == 0:
        return []
    n = max(1, len(y) // points)
    trimmed = y[: (len(y) // n) * n]
    env = np.abs(trimmed.reshape(-1, n)).max(axis=1)
    m = float(env.max()) or 1.0
    return [round(float(v / m), 3) for v in env]


def analyze_track(path: str | Path) -> BeatGridData:
    y, sr = load_audio(path)
    duration = len(y) / sr
    tempo, beats = detect_beats(y, sr)
    bass_hits = detect_bass_hits(y, sr, beats)
    drops = detect_drop(y, sr, beats, bass_hits)
    chosen = drops[0].t if drops else None
    downbeats = pick_downbeats(beats, y, sr, chosen)
    low = _lowpass(y, sr)
    per_sec = [round(float(np.sqrt(np.mean(low[i * sr:(i + 1) * sr] ** 2))), 5) for i in range(int(duration))]
    return BeatGridData(
        tempo=round(tempo, 3), duration=round(duration, 3), beats=beats, downbeats=[round(d, 4) for d in downbeats],
        bass_hits=bass_hits, drop_candidates=[d.to_dict() for d in drops], chosen_drop=chosen,
        waveform=waveform_envelope(y), low_energy=per_sec,
    )


def auto_window(grid: BeatGridData, target_length: float | None, drop_position: float = 0.6) -> dict:
    """Song window: the drop sits at `drop_position` of the window (payoff after it). Snapped to beats."""
    duration = grid.duration
    if target_length is None or target_length <= 0:
        length = min(60.0, duration)
    else:
        length = min(float(target_length), duration)
    drop = grid.chosen_drop if grid.chosen_drop is not None else duration * 0.5
    start = drop - drop_position * length
    start = max(0.0, min(start, duration - length))
    start = _snap(start, grid.beats, max_dist=grid.beat_period())
    end = min(duration, start + length)
    return {"start": round(start, 3), "end": round(end, 3)}

"""Candidate moments: motion peaks inside shots, with in/out windows, crop paths, and scores.

Vision tags (species, action, intensity, framing) are added later by `analysis/vision.py`;
`score_moment` combines everything into the final score.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict

from wildcut.analysis.motion import MotionCurve
from wildcut.analysis.shots import Shot
from wildcut.analysis.tracking import build_crop_path

PRE_PEAK = 1.2    # seconds of lead-in before the peak
POST_PEAK = 1.0   # seconds of tail after the peak
MIN_MOMENT = 0.6
CUT_GUARD = 0.2   # seconds around an internal cut where motion peaks are ignored
ACTIONS = ["pounce", "strike", "leap", "swing", "fight", "catch", "dive", "sprint", "escape", "idle"]
# motion value (width-fractions / s) that maps to a motion score of 1.0
MOTION_FULL_SCALE = 0.45


@dataclass
class Moment:
    id: str
    clip_id: str
    in_t: float
    out_t: float
    peak_t: float
    motion_score: float
    species: str = ""
    action: str = ""
    intensity: int = 0
    framing: int = 0
    subject_visible: bool | None = None
    caption_hint: str = ""
    habitat: str = ""
    lighting: str = ""
    dominant_color: str = ""
    outcome: str = ""
    category: str = ""   # hero | aura | broll | other (documentary mode)
    crop_paths: dict[str, dict] = field(default_factory=dict)  # by aspect key, e.g. "9:16"
    score: float = 0.0
    shot_start: float = 0.0
    shot_end: float = 0.0
    kind: str = "peak"   # peak | shot (calm fallback when a shot has no peak)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Moment":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    @property
    def duration(self) -> float:
        return self.out_t - self.in_t


ASPECTS = {"9:16": 9 / 16, "1:1": 1.0, "4:5": 0.8, "3:4": 0.75, "16:9": 16 / 9}


def score_moment(m: Moment) -> float:
    """Weighted motion + intensity + framing; idle and unclear-subject moments are penalized."""
    motion = min(1.0, m.motion_score)
    if m.intensity:
        intensity = m.intensity / 10.0
        framing = (m.framing or 5) / 10.0
        score = 0.45 * motion + 0.35 * intensity + 0.20 * framing
    else:
        score = motion  # no vision tags yet
    if m.action == "idle":
        score *= 0.35
    if m.subject_visible is False:
        score *= 0.4
    if m.kind == "shot":
        score *= 0.6
    return round(float(score), 4)


def build_moments(clip_id: str, shots: list[Shot], motion: MotionCurve, src_aspect: float,
                  aspects: dict[str, float] | None = None, max_per_clip: int = 12) -> list[Moment]:
    aspects = aspects or ASPECTS
    moments: list[Moment] = []
    clip_end = shots[-1].end if shots else (motion.times[-1] if motion.times else 0.0)

    def make(peak_t: float, value: float, shot: Shot, kind: str) -> Moment:
        in_t = max(shot.start, peak_t - PRE_PEAK)
        out_t = min(shot.end, peak_t + POST_PEAK, clip_end)
        if out_t - in_t < MIN_MOMENT:
            # widen toward whichever side has room inside the shot
            out_t = min(shot.end, in_t + MIN_MOMENT)
            in_t = max(shot.start, out_t - MIN_MOMENT)
        m = Moment(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{clip_id}:{in_t:.3f}:{out_t:.3f}:{peak_t:.3f}:{kind}")),
            clip_id=clip_id, in_t=round(in_t, 3), out_t=round(out_t, 3), peak_t=round(peak_t, 3),
            motion_score=round(min(1.0, value / MOTION_FULL_SCALE), 4),
            shot_start=shot.start, shot_end=shot.end, kind=kind,
        )
        fallback = None
        box = motion.box_at(peak_t)
        if box:
            fallback = (box[0] + box[2] / 2, box[1] + box[3] / 2)
        for key, asp in aspects.items():
            m.crop_paths[key] = build_crop_path(motion.times, motion.boxes, m.in_t, m.out_t, src_aspect, asp,
                                                fallback_center=fallback).to_dict()
        m.score = score_moment(m)
        return m

    used_shots: set[int] = set()
    internal_cuts = [s.start for s in shots[1:]]
    for pt, pv in zip(motion.peak_times, motion.peak_values):
        # optical flow across a hard cut is garbage; ignore peaks hugging an internal cut
        if any(abs(pt - c) <= CUT_GUARD for c in internal_cuts):
            continue
        shot_idx = next((i for i, s in enumerate(shots) if s.start - 1e-3 <= pt <= s.end + 1e-3), None)
        if shot_idx is None:
            continue
        used_shots.add(shot_idx)
        moments.append(make(pt, pv, shots[shot_idx], "peak"))
    # calm fallback: shots without a peak get one moment at their strongest point (or middle)
    for i, s in enumerate(shots):
        if i in used_shots or s.duration < MIN_MOMENT:
            continue
        best_t, best_v = (s.start + s.end) / 2, 0.0
        for t, v in zip(motion.times, motion.smoothed or motion.subject):
            if s.start <= t <= s.end and v > best_v:
                best_t, best_v = t, v
        moments.append(make(best_t, best_v, s, "shot"))
    moments.sort(key=lambda m: m.score, reverse=True)
    seen: set[str] = set()
    unique = [m for m in moments if not (m.id in seen or seen.add(m.id))]   # never two rows with one id
    return unique[:max_per_clip]

"""Shot detection with PySceneDetect. Very short shots (<0.4 s) merge with their neighbors."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

MIN_SHOT = 0.4


@dataclass
class Shot:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)


def merge_short_shots(shots: list[Shot], min_len: float = MIN_SHOT) -> list[Shot]:
    if not shots:
        return []
    merged = [Shot(shots[0].start, shots[0].end)]
    for s in shots[1:]:
        if merged[-1].duration < min_len:
            merged[-1] = Shot(merged[-1].start, s.end)
        else:
            merged.append(Shot(s.start, s.end))
    # a trailing short shot folds into its predecessor
    if len(merged) > 1 and merged[-1].duration < min_len:
        last = merged.pop()
        merged[-1] = Shot(merged[-1].start, last.end)
    return merged


def detect_shots(path: str | Path, duration: float | None = None, threshold: float = 27.0) -> list[Shot]:
    """Content-based cut detection. Returns at least one shot spanning the clip."""
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector(threshold=threshold, min_scene_len=int(0.3 * 30)))
    shots: list[Shot] = []
    for start, end in scenes:
        shots.append(Shot(float(start.get_seconds()), float(end.get_seconds())))
    if not shots:
        from wildcut.media import probe

        d = duration if duration is not None else probe(path).duration
        shots = [Shot(0.0, d)]
    elif duration is not None and shots[-1].end < duration - 0.05:
        shots[-1] = Shot(shots[-1].start, duration)
    return merge_short_shots(shots)

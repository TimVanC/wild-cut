"""Beat, bass-hit, and drop detection against the synthetic phonk track."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wildcut.music.analysis import BeatGridData, analyze_track, auto_window


@pytest.fixture(scope="module")
def grid_and_truth(assets):
    gt = assets["track"]
    grid = analyze_track(Path(assets["dir"]) / gt["path"])
    return grid, gt


def _errors(detected, truth):
    det = np.asarray(detected)
    return np.asarray([np.min(np.abs(det - t)) for t in truth])


def test_tempo(grid_and_truth):
    grid, gt = grid_and_truth
    assert abs(grid.tempo - gt["bpm"]) < 1.5


def test_beats_within_50ms(grid_and_truth):
    grid, gt = grid_and_truth
    errs = _errors(grid.beats, gt["beats"])
    assert np.all(errs <= 0.05), f"max beat error {errs.max():.3f}s"
    assert abs(len(grid.beats) - len(gt["beats"])) <= 1


def test_downbeats_within_50ms(grid_and_truth):
    grid, gt = grid_and_truth
    errs = _errors(grid.downbeats, gt["downbeats"])
    assert np.all(errs <= 0.05)
    assert abs(len(grid.downbeats) - len(gt["downbeats"])) <= 1


def test_bass_hits_within_50ms(grid_and_truth):
    grid, gt = grid_and_truth
    errs = _errors(grid.bass_hits, gt["bass_hits"])
    assert np.mean(errs <= 0.05) >= 0.95, f"808 recall too low: {np.mean(errs <= 0.05):.2f}"
    low_events = np.asarray(sorted(set(gt["bass_hits"] + gt["kicks"])))
    precision = np.mean([np.min(np.abs(low_events - h)) <= 0.05 for h in grid.bass_hits])
    assert precision >= 0.95
    # the quiet build must not produce "strong" bass hits
    assert not any(h < gt["drop_time"] - 0.1 for h in grid.bass_hits)


def test_drop_within_50ms_and_is_a_downbeat(grid_and_truth):
    grid, gt = grid_and_truth
    assert grid.chosen_drop is not None
    assert abs(grid.chosen_drop - gt["drop_time"]) <= 0.05
    assert grid.drop_candidates[0]["score"] == 1.0
    assert len(grid.drop_candidates) <= 3
    assert min(abs(d - grid.chosen_drop) for d in grid.downbeats) <= 0.05


def test_auto_window_places_drop_at_60_percent(grid_and_truth):
    grid, _ = grid_and_truth
    w = auto_window(grid, 15)
    assert abs((w["end"] - w["start"]) - 15) < 0.01
    frac = (grid.chosen_drop - w["start"]) / 15
    assert 0.5 <= frac <= 0.7
    full = auto_window(grid, None)
    assert full == {"start": 0.0, "end": 30.0}


def test_grid_roundtrip(grid_and_truth):
    grid, _ = grid_and_truth
    d = grid.to_dict()
    g2 = BeatGridData.from_dict(d)
    assert g2.beats == grid.beats and g2.chosen_drop == grid.chosen_drop
    assert abs(g2.beat_period() - 60 / grid.tempo) < 1e-6

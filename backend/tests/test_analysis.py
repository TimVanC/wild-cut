"""Footage analysis against synthetic ground truth."""
from __future__ import annotations

from pathlib import Path

import pytest

from wildcut.analysis.moments import build_moments
from wildcut.analysis.motion import compute_motion
from wildcut.analysis.shots import Shot, detect_shots, merge_short_shots
from wildcut.analysis.tracking import CropPath, point_in_crop
from wildcut.media import extract_frames, make_proxy, probe


def _clip(assets, name) -> Path:
    return Path(assets["dir"]) / assets["clips"][name]["path"]


def test_probe_reports_dimensions(assets):
    info = probe(_clip(assets, "static_peaks"))
    assert (info.width, info.height) == (960, 540)
    assert abs(info.fps - 30) < 0.01
    assert abs(info.duration - 12.0) < 0.05
    assert info.has_video and not info.has_audio


def test_proxy_is_540p_and_seekable(assets, tmp_path):
    out = make_proxy(_clip(assets, "clip_a"), tmp_path / "clip_a_proxy.mp4")
    info = probe(out)
    assert info.height == 540
    frames = extract_frames(out, [0.5, 2.0], width=320)
    assert len(frames) == 2 and frames[0].shape[1] == 320


def test_motion_peaks_within_200ms(assets):
    for name in ("static_peaks", "clip_a", "clip_b", "clip_c"):
        curve = compute_motion(_clip(assets, name))
        truth = assets["clips"][name]["peaks"]
        for t in truth:
            assert any(abs(p - t) <= 0.2 for p in curve.peak_times), f"{name}: missing peak at {t}, got {curve.peak_times}"
        assert len(curve.peak_times) <= len(truth) + 1


def test_camera_pan_does_not_score_as_motion(assets):
    curve = compute_motion(_clip(assets, "panning"))
    import numpy as np

    sm = np.asarray(curve.smoothed)
    base = float(np.median(sm))
    assert max(curve.camera) > 0.05, "pan should register as camera motion"
    assert base < 0.05, f"pan baseline should be low subject motion, got {base}"
    # the single real burst still shows up
    assert any(abs(p - 6.0) <= 0.2 for p in curve.peak_times)
    assert max(curve.peak_values) > 5 * base


def test_motion_ranks_bursts_by_strength(assets):
    vals = {}
    for name in ("clip_a", "clip_b", "clip_c"):
        c = compute_motion(_clip(assets, name))
        vals[name] = max(c.peak_values)
    # ground truth amplitudes: a=950 > c=650 > b=500
    assert vals["clip_a"] > vals["clip_c"] > vals["clip_b"]


def test_shot_detection_finds_hard_cut(assets):
    path = _clip(assets, "two_shots")
    shots = detect_shots(path, probe(path).duration)
    assert len(shots) == 2
    assert abs(shots[0].end - 4.0) < 0.1
    assert abs(shots[1].end - 8.0) < 0.1


def test_short_shots_merge():
    shots = [Shot(0, 1.0), Shot(1.0, 1.2), Shot(1.2, 3.0), Shot(3.0, 3.1)]
    merged = merge_short_shots(shots)
    assert [(s.start, s.end) for s in merged] == [(0, 1.0), (1.0, 3.1)] or len(merged) == 2


def test_moments_ignore_cut_artifacts_and_keep_subject_in_frame(assets):
    path = _clip(assets, "two_shots")
    info = probe(path)
    shots = detect_shots(path, info.duration)
    curve = compute_motion(path)
    moments = build_moments("two_shots", shots, curve, info.width / info.height)
    peaks = sorted(m.peak_t for m in moments if m.kind == "peak")
    assert not any(abs(p - 4.0) < 0.2 for p in peaks), f"cut artifact leaked as a peak: {peaks}"
    assert any(abs(p - 2.0) <= 0.2 for p in peaks) and any(abs(p - 6.0) <= 0.2 for p in peaks)
    # every peak moment lies inside a single shot
    for m in moments:
        assert m.shot_start - 1e-3 <= m.in_t <= m.out_t <= m.shot_end + 1e-3
    # 9:16 reframing keeps the disc inside the crop at the peak
    gt_pos = assets["clips"]["two_shots"]["peak_positions"]
    for m in moments:
        if m.kind != "peak":
            continue
        key = next((k for k in gt_pos if abs(float(k) - m.peak_t) <= 0.2), None)
        assert key is not None
        cp = CropPath.from_dict(m.crop_paths["9:16"])
        assert point_in_crop(cp, m.peak_t, gt_pos[key]["x"], gt_pos[key]["y"]), (m.peak_t, cp.center_at(m.peak_t), gt_pos[key])


def test_subject_stays_in_frame_static_clip(assets):
    path = _clip(assets, "static_peaks")
    info = probe(path)
    shots = detect_shots(path, info.duration)
    curve = compute_motion(path)
    moments = build_moments("static", shots, curve, info.width / info.height)
    gt_pos = assets["clips"]["static_peaks"]["peak_positions"]
    checked = 0
    for m in moments:
        key = next((k for k in gt_pos if abs(float(k) - m.peak_t) <= 0.2), None)
        if key is None:
            continue
        for aspect in ("9:16", "1:1", "4:5"):
            cp = CropPath.from_dict(m.crop_paths[aspect])
            assert point_in_crop(cp, m.peak_t, gt_pos[key]["x"], gt_pos[key]["y"], margin=0.02)
        checked += 1
    assert checked == 2

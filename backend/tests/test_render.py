"""Renderer: deterministic output, correct geometry, title on the drop frame, no black edges."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from wildcut.media import extract_frames, probe
from wildcut.render.composer import RenderSettings, render, render_frame, render_video
from wildcut.render.text import render_title_layer

from tests.test_planner import _req, footage, grid  # noqa: F401  (fixtures)
from wildcut.planner.planner import plan


@pytest.fixture(scope="module")
def phonk_edl(footage, grid):  # noqa: F811
    return plan(_req(footage, grid, target_length=8.0, seed=2))


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_render_is_deterministic(phonk_edl, tmp_path):
    a = render_video(phonk_edl, tmp_path / "a.mp4", RenderSettings(quality="preview", width=270))
    b = render_video(phonk_edl, tmp_path / "b.mp4", RenderSettings(quality="preview", width=270))
    assert _md5(a) == _md5(b)
    info = probe(a)
    assert (info.width, info.height) == (270, 480)
    assert abs(info.duration - phonk_edl["duration"]) < 0.15


def test_title_visible_on_drop_frame(phonk_edl):
    import copy

    drop = phonk_edl["markers"]["drop"]
    fps = phonk_edl["fps"]
    t = drop + 6 / fps
    with_title = render_frame(phonk_edl, t, width=270)
    no_title = copy.deepcopy(phonk_edl)
    no_title["text"] = []
    without = render_frame(no_title, t, width=270)
    diff = np.abs(with_title.astype(int) - without.astype(int)).max(axis=2)
    h = diff.shape[0]
    band = diff[int(h * 0.4):int(h * 0.6)]
    assert np.mean(band > 40) > 0.015, "title should change a visible share of the center band"
    assert np.mean(diff[: int(h * 0.3)] > 40) < 0.01, "title should not touch the top of the frame"
    # before the drop there is no title
    before = render_frame(phonk_edl, drop - 12 / fps, width=270)
    no_title_before = render_frame(no_title, drop - 12 / fps, width=270)
    assert np.array_equal(before, no_title_before)


def test_flash_frame_is_white(phonk_edl):
    drop = phonk_edl["markers"]["drop"]
    f = render_frame(phonk_edl, drop, width=180)
    assert f.mean() > 200


def test_shake_never_shows_black_edges(phonk_edl):
    # sample every shake onset and the frame after; corners must not be pure black
    shakes = [e for e in phonk_edl["effects"] if e["type"] == "shake"][:6]
    fps = phonk_edl["fps"]
    for e in shakes:
        for dt in (0, 1 / fps):
            f = render_frame(phonk_edl, e["t"] + dt, width=180)
            corners = [f[:4, :4], f[:4, -4:], f[-4:, :4], f[-4:, -4:]]
            assert all(c.max() > 8 for c in corners), f"black corner at t={e['t'] + dt}"


def test_segment_render_matches_full(phonk_edl, tmp_path):
    seg = render_video(phonk_edl, tmp_path / "seg.mp4", RenderSettings(quality="preview", width=180), segment=(1.0, 2.0))
    info = probe(seg)
    assert abs(info.duration - 1.0) < 0.1
    full = render_video(phonk_edl, tmp_path / "full.mp4", RenderSettings(quality="preview", width=180))
    fa = extract_frames(full, [1.5])[0]
    fs = extract_frames(seg, [0.5])[0]
    assert np.abs(fa.astype(int) - fs.astype(int)).mean() < 6


def test_mixed_export_has_aac_audio(phonk_edl, assets, tmp_path):
    e = dict(phonk_edl)
    e["audio"] = dict(e["audio"], export="mixed", song_path=str(Path(assets["dir"]) / assets["track"]["path"]))
    out = render(e, tmp_path / "mixed.mp4", RenderSettings(quality="preview", width=180))
    info = probe(out)
    assert info.has_audio and info.has_video
    silent = render(phonk_edl, tmp_path / "silent.mp4", RenderSettings(quality="preview", width=180))
    assert not probe(silent).has_audio


def test_title_layer_renders_serif():
    layer = render_title_layer("THE GIBBON", "Cinzel-Bold.ttf", 60, 0.16, "#FFFFFF", 0.5, 0, 1000)
    arr = np.asarray(layer)
    assert arr[..., 3].max() == 255 and layer.width > 300


def test_cinematic_render_is_square(footage, grid, tmp_path):  # noqa: F811
    e = plan(_req(footage, grid, style="cinematic", aspect="1:1", target_length=8.0))
    out = render_video(e, tmp_path / "cine.mp4", RenderSettings(quality="preview", width=200))
    info = probe(out)
    assert (info.width, info.height) == (200, 200)

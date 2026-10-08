"""Planner: hero on the drop, cuts on beats, effects on hits, locks survive regenerate."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from wildcut.analysis.moments import build_moments
from wildcut.analysis.motion import compute_motion
from wildcut.analysis.shots import detect_shots
from wildcut.analysis.vision import heuristic_tags
from wildcut.media import probe
from wildcut.music.analysis import analyze_track
from wildcut.planner import edl as edlmod
from wildcut.planner.planner import ClipInfo, PlanRequest, plan
from wildcut.planner.speed import hero_ramp, source_at, timeline_between, timeline_duration

FPS = 30
CLIP_NAMES = ["clip_a", "clip_b", "clip_c", "static_peaks", "two_shots", "panning"]


@pytest.fixture(scope="module")
def footage(assets):
    clips, moments = [], []
    for i, name in enumerate(CLIP_NAMES):
        path = Path(assets["dir"]) / assets["clips"][name]["path"]
        info = probe(path)
        shots = detect_shots(path, info.duration)
        curve = compute_motion(path)
        ms = build_moments(name, shots, curve, info.width / info.height)
        for m in ms:
            heuristic_tags(m)
            m.species = "gibbon"
        moments += ms
        clips.append(ClipInfo(id=name, label=f"Clip {i + 1}", path=str(path), proxy=str(path), duration=info.duration,
                              width=info.width, height=info.height, species="gibbon"))
    return clips, moments


@pytest.fixture(scope="module")
def grid(assets):
    return analyze_track(Path(assets["dir"]) / assets["track"]["path"])


def _req(footage, grid, **kw):
    clips, moments = footage
    base = dict(project_id="p1", style="phonk", aspect="9:16", mode="music", target_length=15.0, seed=1,
                clips=clips, moments=copy.deepcopy(moments), grid=grid, song_path="song.wav", song_window=None)
    base.update(kw)
    return PlanRequest(**base)


# ---------------------------------------------------------------- speed curve math

def test_speed_curve_roundtrip():
    keys = hero_ramp(5.0, slow_rate=0.4)
    d = timeline_duration(3.0, 7.0, keys)
    assert d > 4.0  # slow-mo stretches time
    s = source_at(keys, 3.0, 7.0, timeline_between(keys, 3.0, 5.0))
    assert abs(s - 5.0) < 1e-5
    assert abs(timeline_duration(3.0, 7.0, None) - 4.0) < 1e-9


# ---------------------------------------------------------------- music mode

def test_hero_peak_lands_on_drop(footage, grid):
    e = plan(_req(footage, grid))
    assert e["markers"]["drop"] is not None
    hero = next(c for c in e["clips"] if c["role"] == "hero")
    assert hero["speed"], "hero should have a speed ramp"
    peak_tl = edlmod.peak_timeline(hero)
    assert abs(peak_tl - e["markers"]["drop"]) <= 1.0 / FPS, (peak_tl, e["markers"]["drop"])
    assert abs(e["markers"]["hero_peak"] - e["markers"]["drop"]) <= 1.0 / FPS
    # the hero is the highest-scoring moment
    hero_m = next(m for m in footage[1] if m.id == hero["moment_id"])
    assert hero_m.score >= max(m.score for m in footage[1]) - 1e-6


def test_cuts_land_on_beats(footage, grid):
    e = plan(_req(footage, grid))
    beats = e["markers"]["beats"]
    half = sorted(set(beats + [(a + b) / 2 for a, b in zip(beats, beats[1:])]))
    for c in e["clips"]:
        if c["role"] == "hero":
            continue
        assert min(abs(c["start"] - b) for b in half) <= 1.0 / FPS, f"cut at {c['start']} is off the beat grid"
    assert len(e["clips"]) >= 6


def test_duration_matches_window_and_offset_reported(footage, grid):
    e = plan(_req(footage, grid))
    w = e["audio"]["song_window"]
    assert abs((w["end"] - w["start"]) - 15.0) < 0.01
    assert abs(e["duration"] - 15.0) < 0.2
    assert e["audio"]["sound_offset"] == w["start"]
    assert e["audio"]["export"] == "silent"


def test_effects_on_bass_hits_and_downbeats(footage, grid):
    e = plan(_req(footage, grid))
    hits = e["markers"]["bass_hits"]
    downs = e["markers"]["downbeats"]
    shakes = [f for f in e["effects"] if f["type"] == "shake"]
    assert len(shakes) == len([h for h in hits if h <= e["duration"]])
    for s in shakes:
        assert min(abs(s["t"] - h) for h in hits) < 1e-6
    flashes = [f for f in e["effects"] if f["type"] == "flash"]
    assert any(abs(f["t"] - e["markers"]["drop"]) < 1e-6 for f in flashes)
    zooms = [f for f in e["effects"] if f["type"] == "zoom_punch"]
    assert all(min(abs(z["t"] - d) for d in downs) < 1e-6 for z in zooms)


def test_title_is_only_text_and_sits_on_drop(footage, grid):
    e = plan(_req(footage, grid))
    assert len(e["text"]) == 1
    t = e["text"][0]
    assert t["text"] == "THE GIBBON"
    assert abs(t["t"] - e["markers"]["drop"]) <= 1.0 / FPS
    assert t["style"]["font"].lower().startswith("cinzel")


def test_no_moment_reused_when_pool_is_large(footage, grid):
    clips, moments = footage
    # synthesize a large pool: every real moment cloned at different ids (distinct "moments" of the same clips)
    big = []
    for k in range(6):
        for m in moments:
            mm = copy.deepcopy(m)
            mm.id = f"{m.id}-{k}"
            mm.peak_t = max(0.3, min(clips[[c.id for c in clips].index(m.clip_id)].duration - 0.3, m.peak_t + 0.05 * k))
            big.append(mm)
    e = plan(_req(footage, grid, moments=big))
    ids = [c["moment_id"] for c in e["clips"]]
    assert len(ids) == len(set(ids))
    assert not any("reused" in n for n in e["notes"])


def test_small_pool_reuses_and_says_so(footage, grid):
    e = plan(_req(footage, grid))
    ids = [c["moment_id"] for c in e["clips"]]
    assert len(ids) > len(set(ids))
    assert any("reused" in n for n in e["notes"])


def test_deterministic_and_seed_varies(footage, grid):
    a = plan(_req(footage, grid, seed=3))
    b = plan(_req(footage, grid, seed=3))
    assert [c["moment_id"] for c in a["clips"]] == [c["moment_id"] for c in b["clips"]]
    assert [(f["type"], f["t"]) for f in a["effects"]] == [(f["type"], f["t"]) for f in b["effects"]]
    c = plan(_req(footage, grid, seed=4))
    assert [x["moment_id"] for x in a["clips"]] != [x["moment_id"] for x in c["clips"]]


def test_locks_survive_regenerate(footage, grid):
    e = plan(_req(footage, grid, seed=1))
    # user pins: clip order "two_shots, clip_b, clip_c" with the title on a specific time, window locked
    e["clips"] = [c for c in e["clips"]]
    first = [c for c in e["clips"] if c["clip_id"] == "two_shots"][:1]
    second = [c for c in e["clips"] if c["clip_id"] == "clip_b"][:1]
    third = [c for c in e["clips"] if c["clip_id"] == "clip_c"][:1]
    assert first and second and third
    locked = [first[0], second[0], third[0]]
    for c in locked:
        c["locked_order"] = True
    locked[1]["locked_range"] = True
    kept_range = (locked[1]["in"], locked[1]["out"])
    others = [c for c in e["clips"] if c not in locked]
    e["clips"] = locked + others
    e["text"][0]["locked"] = True
    e["text"][0]["t"] = 4.0
    e["audio"]["window_locked"] = True
    regenerated = plan(_req(footage, grid, seed=9, existing=e))
    order = [c["clip_id"] for c in regenerated["clips"]]
    idx = [order.index(cid) for cid in ("two_shots", "clip_b", "clip_c")]
    assert idx == sorted(idx), f"locked order broken: {order}"
    assert order[:3] == ["two_shots", "clip_b", "clip_c"]
    kept = next(c for c in regenerated["clips"] if c["clip_id"] == "clip_b")
    assert (kept["in"], kept["out"]) == kept_range and kept["locked_range"]
    assert regenerated["text"][0]["t"] == 4.0 and regenerated["text"][0]["locked"]
    assert regenerated["audio"]["song_window"] == e["audio"]["song_window"]
    # the hero still lands on the drop
    hero = next(c for c in regenerated["clips"] if c["role"] == "hero")
    assert abs(edlmod.peak_timeline(hero) - regenerated["markers"]["drop"]) <= 1.0 / FPS


def test_fully_locked_order_is_exact(footage, grid):
    e = plan(_req(footage, grid, seed=1))
    want = ["clip_b", "clip_c", "clip_a"]
    picked = [next(c for c in e["clips"] if c["clip_id"] == cid) for cid in want]
    for c in picked:
        c["locked_order"] = True
    e["clips"] = picked
    out = plan(_req(footage, grid, seed=5, existing=e))
    assert [c["clip_id"] for c in out["clips"]] == want
    hero = next(c for c in out["clips"] if c["role"] == "hero")
    scores = {c["clip_id"]: next(m.score for m in footage[1] if m.id == c["moment_id"]) for c in picked}
    assert hero["clip_id"] == max(scores, key=scores.get)
    assert abs(edlmod.peak_timeline(hero) - out["markers"]["drop"]) <= 1.0 / FPS
    assert any("Moved the song window" in n for n in out["notes"])


# ---------------------------------------------------------------- visual mode

def test_visual_mode_effects_on_peaks_no_music(footage, grid):
    e = plan(_req(footage, None, mode="visual", song_path=None, target_length=20.0))
    assert e["markers"]["drop"] is None and e["markers"]["beats"] == []
    assert 14.0 <= e["duration"] <= 21.0
    peaks = [edlmod.peak_timeline(c) for c in e["clips"]]
    for f in e["effects"]:
        if f["type"] in ("shake", "flash", "zoom_punch"):
            assert min(abs(f["t"] - p) for p in peaks) < 1e-6
    hero = next(c for c in e["clips"] if c["role"] == "hero")
    assert hero["speed"]
    assert abs(e["text"][0]["t"] - edlmod.peak_timeline(hero)) <= 1.0 / FPS
    # arc: scores rise toward the hero
    before = [c for c in e["clips"] if c["start"] < hero["start"]]
    scores = [next(m.score for m in footage[1] if m.id == c["moment_id"]) for c in before]
    assert scores == sorted(scores)


# ---------------------------------------------------------------- cinematic

def test_cinematic_square_serif_no_shake(footage, grid):
    e = plan(_req(footage, grid, style="cinematic", aspect="1:1", target_length=20.0))
    assert e["aspect"] == "1:1"
    assert not any(f["type"] in ("shake", "flash", "chromatic", "zoom_punch") for f in e["effects"])
    assert any(f["type"] == "push_in" for f in e["effects"])
    t = e["text"][0]
    assert t["animation"] == "fade" and "cormorant" in t["style"]["font"].lower()
    for c in e["clips"]:
        if c["role"] != "hero":
            assert 2.5 <= c["tl_duration"] <= 8.5, c
    hero = next(c for c in e["clips"] if c["role"] == "hero")
    assert hero["speed"][1]["rate"] == 0.7


def test_relayout_and_validate(footage, grid):
    e = plan(_req(footage, grid))
    assert edlmod.validate(e) == []
    e2 = copy.deepcopy(e)
    e2["clips"][0]["out"] += 0.5
    edlmod.relayout(e2)
    assert abs(e2["duration"] - (e["duration"] + 0.5)) < 1e-6
    assert edlmod.clip_at(e2, 0.1)["id"] == e2["clips"][0]["id"]


def test_frame_survives_regenerate_and_project_default(footage, grid):
    from wildcut.services import edl_ops

    e = plan(_req(footage, grid, seed=3, options={"frame": 1.2}))
    assert all(c["frame"] == 1.2 for c in e["clips"]), "project default framing applies to every clip"
    # one pinned clip switched back to fill by hand keeps that across a re-plan with the same default
    pinned = e["clips"][1]
    edl_ops.set_frame(e, pinned["id"], "fill")
    edl_ops.set_lock(e, pinned["id"], True)
    assert pinned["frame"] is None
    out = plan(_req(footage, grid, seed=4, existing=e, options={"frame": 1.2}))
    kept = next(c for c in out["clips"] if c["id"] == pinned["id"])
    assert kept["frame"] is None
    others = [c for c in out["clips"] if c["id"] != pinned["id"]]
    assert others and all(c["frame"] == 1.2 for c in others)
    # "apply to all" labels every clip; a bad aspect is rejected
    note = edl_ops.set_frame(out, None, "16:9")
    assert "every clip" in note and all(abs(c["frame"] - 16 / 9) < 1e-6 for c in out["clips"])
    with pytest.raises(edl_ops.EdlOpError):
        edl_ops.set_frame(out, None, "9:1")


def test_focus_makes_the_subject_the_hero_and_the_title(footage, grid):
    """Tim's iguana clip: the snakes' pounce out-scores everything, but the edit is about the iguana escaping."""
    clips, moments = footage
    import copy

    ms = copy.deepcopy(moments)
    best = max(ms, key=lambda m: m.score)
    for m in ms:
        m.species, m.action, m.caption_hint = "snake", "pounce", "group of snakes lunge and coil in a frenzy"
    # one modest moment of the subject doing its thing, on a different clip than the top snake moment
    weak = min((m for m in ms if m.clip_id != best.clip_id), key=lambda m: m.score)
    weak.species, weak.action, weak.caption_hint = "marine iguana", "escape", "baby iguana dashes away from the snakes"
    plain = plan(_req(footage, grid, seed=3, moments=ms))
    hero = next(c for c in plain["clips"] if c["role"] == "hero")
    assert hero["moment_id"] != weak.id and hero["species"] == "snake"   # without a focus the snakes win
    focused = plan(_req(footage, grid, seed=3, moments=copy.deepcopy(ms), options={"focus": {"subject": "iguana", "action": "escapes"}}))
    hero = next(c for c in focused["clips"] if c["role"] == "hero")
    assert hero["moment_id"] == weak.id, "the subject's key action must be the hero"
    assert focused["text"][0]["text"] == "THE IGUANA"
    # the iguana moment also outranks snake moments elsewhere in the edit (plural/verb forms match)
    from wildcut.planner.planner import _words

    assert _words("The iguanas escaping the snakes") == {"iguana", "escap", "snake"} or "iguana" in _words("iguanas")

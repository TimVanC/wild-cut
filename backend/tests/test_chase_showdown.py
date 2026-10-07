"""Chase pairing + three-beat structure; Showdown cards, timing, stats gate, stats.csv."""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest

from wildcut.media import probe
from wildcut.planner import edl as edlmod
from wildcut.planner import plan_for_preset
from wildcut.planner.chase import classify
from wildcut.planner.planner import PlanRequest
from wildcut.planner.presets import load_preset
from wildcut.planner.showdown import blockers, plan_showdown, write_stats_csv
from wildcut.render.composer import RenderSettings, render_frame, render_video

from tests.test_planner import footage, grid  # noqa: F401

FPS = 30


def _chase_req(footage, grid, **kw):  # noqa: F811
    clips, moments = footage
    moments = copy.deepcopy(moments)
    # tag by clip: clip_a/static_peaks = cheetah (savanna), clip_b/two_shots = gazelle (savanna), others = forest
    for m in moments:
        if m.clip_id in ("clip_a", "static_peaks"):
            m.species, m.action, m.habitat, m.dominant_color = "cheetah", "sprint", "savanna", "yellow"
        elif m.clip_id in ("clip_b", "two_shots"):
            m.species, m.action, m.habitat, m.dominant_color = "gazelle", "sprint", "savanna", "yellow"
        else:
            m.species, m.habitat = "gibbon", "forest"
    hero = max((m for m in moments if m.clip_id == "clip_a"), key=lambda m: m.score)
    hero.action, hero.outcome = "pounce", "caught"
    base = dict(project_id="p2", style="chase", aspect="3:4", mode="music", target_length=15.0, seed=1,
                clips=clips, moments=moments, grid=grid, song_path="song.wav", song_window=None)
    base.update(kw)
    return PlanRequest(**base)


def test_chase_classify():
    p = load_preset("chase")
    assert classify("cheetah", p) == "predator" and classify("gazelle", p) == "prey" and classify("gibbon", p) == "unknown"
    assert p["effects"]["shake"] is not None, "Chase inherits the full Phonk treatment"


def test_chase_pairs_and_three_beats(footage, grid):  # noqa: F811
    e = plan_for_preset(_chase_req(footage, grid))
    assert e["chase"]["variant"] == "caught"
    assert e["chase"]["predator"] == "cheetah" and e["chase"]["prey"] == "gazelle"
    hero = next(c for c in e["clips"] if c["role"] == "hero")
    assert hero["species"] == "cheetah"
    assert abs(edlmod.peak_timeline(hero) - e["markers"]["drop"]) <= 1 / FPS
    build = [c for c in e["clips"] if c["start"] < hero["start"]]
    species = [c["species"] for c in build]
    assert "cheetah" in species and "gazelle" in species, species
    assert "gibbon" not in [c["species"] for c in e["clips"]], "scene-mismatched clips should not be used"
    assert any(f["type"] == "motion_blur" for f in e["effects"])
    assert any(f["type"] == "fade_black" and f["params"].get("hard") for f in e["effects"])
    titles = [t for t in e["text"] if t.get("kind") != "marker"]
    assert len(titles) == 1 and titles[0]["text"] == "THE CHEETAH"
    assert 10 <= e["duration"] <= 20.5
    assert any(f["type"] == "shake" for f in e["effects"])


def test_chase_escape_variant(footage, grid):  # noqa: F811
    req = _chase_req(footage, grid, options={"chase_variant": "escaped"})
    e = plan_for_preset(req)
    assert e["chase"]["variant"] == "escaped"
    hero = next(c for c in e["clips"] if c["role"] == "hero")
    assert hero["species"] == "gazelle"
    assert e["text"][0]["text"] == "THE GAZELLE"
    assert not any(f["type"] == "motion_blur" for f in e["effects"])


# ---------------------------------------------------------------- showdown

ROWS = [
    {"animal": "cheetah", "value": 110.0, "unit": "km/h", "source_url": "https://en.wikipedia.org/wiki/Cheetah", "fact": "Fastest land animal over short sprints.", "confirmed": False},
    {"animal": "pronghorn", "value": 88.0, "unit": "km/h", "source_url": "https://en.wikipedia.org/wiki/Pronghorn", "fact": "Can hold high speed for miles.", "confirmed": False},
    {"animal": "peregrine falcon", "value": 389.0, "unit": "km/h", "source_url": "https://en.wikipedia.org/wiki/Peregrine_falcon", "fact": "Dives faster than any other animal.", "confirmed": False},
    {"animal": "sailfish", "value": 109.0, "unit": "km/h", "source_url": "", "fact": "", "confirmed": True},
    {"animal": "lion", "value": 80.0, "unit": "km/h", "source_url": "https://en.wikipedia.org/wiki/Lion", "fact": "Short bursts only.", "confirmed": False},
]


def _media(assets):
    d = Path(assets["dir"])
    return {"cheetah": {"src": str(d / "clip_a.mp4")}, "pronghorn": {"src": str(d / "clip_b.mp4")},
            "peregrine falcon": {"src": str(d / "clip_c.mp4")}, "sailfish": {"src": str(d / "panning.mp4")},
            "lion": {"src": str(d / "static_peaks.mp4")}}


def test_stats_gate_blocks_unsourced():
    rows = copy.deepcopy(ROWS)
    assert blockers(rows) == []
    rows[0]["source_url"] = ""
    assert blockers(rows) == ["cheetah"]
    rows[0]["confirmed"] = True
    assert blockers(rows) == []
    rows[1]["value"] = None
    assert "pronghorn" in blockers(rows)


def test_showdown_music_timing_and_cards(assets, grid):  # noqa: F811
    e = plan_showdown("p3", "9:16", "music", 1, {"stat": "top_speed", "unit": "km/h", "stats": ROWS}, grid, "song.wav", None,
                      "silent", _media(assets))
    cards = [c for c in e["clips"] if c.get("kind") == "card"]
    assert [c["card"]["type"] for c in cards] == ["intro", "versus", "versus", "versus", "versus", "winner"]
    names = [a["name"] for a in e["showdown"]["animals"]]
    assert names[-1] == "peregrine falcon" and names[0] == "lion"
    winner = next(c for c in cards if c["card"]["type"] == "winner")
    assert abs(winner["start"] - e["markers"]["drop"]) < 1e-6, "winner reveal lands on the drop"
    for c in cards:
        if c["card"]["type"] == "versus":
            assert min(abs(c["start"] - d) for d in e["markers"]["downbeats"]) < 1e-6, "challengers enter on downbeats"
            assert c["card"]["loser"] == "left"
            assert c["card"]["stamp"] == "SLOW"
    assert e["showdown"]["winner_header"] == "FASTEST ANIMAL"
    assert e["text"] == [], "Showdown headers are drawn by the cards, not text items"
    assert any(f["type"] == "flash" for f in e["effects"])


def test_showdown_visual_fixed_rhythm(assets):
    e = plan_showdown("p3", "9:16", "visual", 1, {"stat": "weight", "unit": "kg", "stats": ROWS[:3]}, None, None, None,
                      "silent", _media(assets))
    versus = [c for c in e["clips"] if c.get("kind") == "card" and c["card"]["type"] == "versus"]
    assert len(versus) == 2
    assert all(abs(c["tl_duration"] - 1.6) < 1e-6 for c in versus)
    assert e["showdown"]["stamp"] == "SMALL"


def test_showdown_renders_cards(assets, grid, tmp_path):  # noqa: F811
    e = plan_showdown("p3", "9:16", "music", 1, {"stat": "top_speed", "unit": "km/h", "stats": ROWS[:3]}, grid, "song.wav", None,
                      "silent", _media(assets))
    versus = next(c for c in e["clips"] if c.get("kind") == "card" and c["card"]["type"] == "versus")
    f_start = render_frame(e, versus["start"] + 0.05, width=270)
    f_end = render_frame(e, versus["start"] + versus["tl_duration"] * 0.85, width=270)
    assert f_start.shape == (480, 270, 3)
    # the frame is essentially greyscale chrome + B&W media
    diff = np.abs(f_end[..., 0].astype(int) - f_end[..., 2].astype(int)).mean()
    assert diff < 12
    assert not np.array_equal(f_start, f_end)
    out = render_video(e, tmp_path / "showdown.mp4", RenderSettings(quality="preview", width=180), segment=(0.0, 1.0))
    assert probe(out).duration > 0.9


def test_stats_csv(tmp_path):
    p = write_stats_csv(ROWS, "top speed", tmp_path / "stats.csv")
    text = p.read_text(encoding="utf-8")
    assert "peregrine falcon" in text and "https://en.wikipedia.org/wiki/Cheetah" in text
    assert text.splitlines()[0].startswith("animal,top speed,unit,source_url")

"""Director tools on a real project: exact order, anchored title near the jump, locks through Regenerate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

from wildcut.db import Project, get_engine
from wildcut.director.tools import Context, resolve_clip, run_tool
from wildcut.planner import edl as edlmod
from wildcut.services.analysis import analyze_project
from wildcut.services.planning import current_edl, load_moments, plan_project
from wildcut.services.projects import add_clip_from_path, project_clips

FPS = 30


@pytest.fixture()
def project(db, assets, tmp_path, monkeypatch):
    from wildcut.claude import ClaudeClient, set_client_for_tests
    from wildcut.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.ensure_dirs()
    set_client_for_tests(ClaudeClient(api_key="", model="x"))
    with Session(get_engine()) as s:
        p = Project(name="director", style="phonk", aspect="9:16", target_length="15", mode="music",
                    song_path=str(Path(assets["dir"]) / assets["track"]["path"]))
        s.add(p)
        s.commit()
        s.refresh(p)
        for name in ("clip_a", "clip_b", "clip_c"):
            add_clip_from_path(s, p, Path(assets["dir"]) / assets["clips"][name]["path"])
        analyze_project(s, p)
        plan_project(s, p, keep_locks=False)
        pid = p.id
    yield pid
    set_client_for_tests(None)


def _ctx(s: Session, pid: str) -> Context:
    p = s.get(Project, pid)
    row = current_edl(s, p)
    return Context(s=s, project=p, edl=json.loads(json.dumps(row.json)), clips=project_clips(s, pid), moments=load_moments(s, pid))


def test_order_title_anchor_and_regenerate(project, assets):
    with Session(get_engine()) as s:
        ctx = _ctx(s, project)
        jump = assets["clips"]["clip_b"]["peaks"][0]   # clip 2 = clip_b, its burst is "the jump"
        res, err = run_tool(ctx, "set_order", {"clips": [2, 3, 1]})
        assert not err, res
        res, err = run_tool(ctx, "set_title", {"time": {"clip": 2, "source_time": jump}})
        assert not err, res
        edl = ctx.edl
        assert [c["label"] for c in edl["clips"]] == ["Clip 2", "Clip 3", "Clip 1"]
        title = edl["text"][0]
        assert title["locked"] and title["anchor"]["source_time"] == jump
        c2 = edl["clips"][0]
        assert c2["in"] <= jump <= c2["out"], "the clip range was pulled to include the jump"
        from wildcut.planner.speed import timeline_between

        expected = c2["start"] + timeline_between(c2.get("speed"), c2["in"], jump)
        assert abs(title["t"] - expected) <= 0.3
        # regenerate with a new seed: order, title anchor, and the hero on the drop all survive
        p = s.get(Project, project)
        row = plan_project(s, p, seed=p.seed + 7, keep_locks=True)
        e2 = row.json
        assert [c["label"] for c in e2["clips"]] == ["Clip 2", "Clip 3", "Clip 1"]
        t2 = e2["text"][0]
        c2b = e2["clips"][0]
        assert c2b["in"] <= jump <= c2b["out"]
        expected2 = c2b["start"] + timeline_between(c2b.get("speed"), c2b["in"], jump)
        assert abs(t2["t"] - expected2) <= 0.3, (t2["t"], expected2)
        # the anchored title's clip became the hero and the drop lands on the jump
        hero = next(c for c in e2["clips"] if c["role"] == "hero")
        assert hero["label"] == "Clip 2"
        assert abs(edlmod.peak_timeline(hero) - e2["markers"]["drop"]) <= 1.0 / FPS
        assert abs(t2["t"] - e2["markers"]["drop"]) <= 0.3


def test_clip_reference_resolution(project):
    with Session(get_engine()) as s:
        ctx = _ctx(s, project)
        assert resolve_clip(ctx, 2).label == "Clip 2"
        assert resolve_clip(ctx, "clip 3").label == "Clip 3"
        assert resolve_clip(ctx, ctx.clips[0].id).label == "Clip 1"
        res, err = run_tool(ctx, "get_moments", {"clip": "clip 9"})
        assert err and "no Clip 9" in res


def test_look_at_returns_frames_and_undo(project):
    with Session(get_engine()) as s:
        ctx = _ctx(s, project)
        res, err = run_tool(ctx, "look_at", {"clip": 1, "start": 1.0, "end": 3.0})
        assert not err
        imgs = [b for b in res if b.get("type") == "image"]
        assert 2 <= len(imgs) <= 12
        v0 = ctx.edl["version"]
        res, err = run_tool(ctx, "add_effect", {"type": "flash", "time": "drop"})
        assert not err
        assert ctx.versions and ctx.versions[-1] > v0
        res, err = run_tool(ctx, "undo", {})
        assert not err and json.loads(res)["version"] == v0

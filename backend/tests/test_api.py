"""End-to-end API flow on the synthetic assets (jobs run in-process via run_pending)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from wildcut.worker import run_pending


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    from wildcut.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "exports_dir", tmp_path / "exports")
    monkeypatch.setattr(settings, "inbox_dir", tmp_path / "inbox")
    settings.ensure_dirs()
    from wildcut.api.app import app

    return TestClient(app)


def _setup_project(client, assets, mode="music", style="phonk", clips=("clip_a", "clip_b", "clip_c", "two_shots")):
    r = client.post("/api/projects", json={"name": "Synthetic Gibbon", "style": style, "target_length": "15", "mode": mode})
    assert r.status_code == 200, r.text
    p = r.json()
    for name in clips:
        rr = client.post(f"/api/projects/{p['id']}/clips", json={"path": str(Path(assets["dir"]) / assets["clips"][name]["path"])})
        assert rr.status_code == 200, rr.text
    if mode == "music":
        rr = client.post(f"/api/projects/{p['id']}/song", json={"path": str(Path(assets["dir"]) / assets["track"]["path"])})
        assert rr.status_code == 200, rr.text
    return p


def test_full_flow_music_phonk(client, assets, tmp_path):
    p = _setup_project(client, assets)
    pid = p["id"]
    assert client.get(f"/api/projects/{pid}/clips").json()[0]["label"] == "Clip 1"
    r = client.post(f"/api/projects/{pid}/analyze")
    assert r.status_code == 200
    run_pending()
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["status"] in ("planned", "analyzed"), proj["message"]
    grid = client.get(f"/api/projects/{pid}/beatgrid").json()
    assert abs(grid["chosen_drop"] - assets["track"]["drop_time"]) < 0.05
    moments = client.get(f"/api/projects/{pid}/moments").json()
    assert len(moments) >= 4
    e = client.get(f"/api/projects/{pid}/edl").json()
    edl = e["edl"]
    hero = next(c for c in edl["clips"] if c["role"] == "hero")
    assert abs(edl["markers"]["hero_peak"] - edl["markers"]["drop"]) <= 1 / 30
    assert edl["audio"]["sound_offset"] == edl["audio"]["song_window"]["start"]
    # the analyze job reports the video and the song as separate stages
    an_job = next(j for j in client.get(f"/api/projects/{pid}/jobs").json() if j["kind"] == "analyze")
    assert an_job["stages"]["video"]["state"] == "done" and an_job["stages"]["song"]["state"] == "done", an_job["stages"]
    assert an_job["stages"]["video"]["progress"] == 1.0
    # preview was rendered by the analyze->plan->preview chain
    pv = client.get(f"/api/projects/{pid}/preview").json()
    assert pv["ready"] and pv["url"].startswith("/api/media?path=")
    media = client.get(pv["url"])
    assert media.status_code == 200 and media.headers["content-type"].startswith("video/mp4")
    # the preview plays the song even though the export is silent
    from wildcut.media import probe

    pv_file = tmp_path / "preview.mp4"
    pv_file.write_bytes(media.content)
    assert probe(pv_file).has_audio, "preview must carry the song"
    ranged = client.get(pv["url"], headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206
    # manual tweak: edit the title text, toggle an effect, swap a moment, trim a clip, then undo
    t = edl["text"][0]
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "set_text", "args": {"text": "THE GIBBON"}, "preview": False})
    assert r.status_code == 200 and r.json()["edl"]["text"][0]["locked"]
    fx = edl["effects"][0]
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "toggle_effect", "args": {"item_id": fx["id"]}, "preview": False})
    assert r.json()["edl"]["effects"][0]["enabled"] is False
    other = next(m for m in moments if m["clip_id"] != hero["clip_id"])
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "swap_moment", "args": {"item_id": hero["id"], "moment_id": other["id"]}, "preview": False})
    assert r.status_code == 200, r.text
    swapped = next(c for c in r.json()["edl"]["clips"] if c["id"] == hero["id"])
    assert swapped["moment_id"] == other["id"] and swapped["locked_range"]
    # framing: one clip boxed 1.2:1, then "apply to all" which also becomes the project default
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "set_frame", "args": {"item_id": hero["id"], "frame": "1.2:1"}, "preview": False})
    assert r.status_code == 200, r.text
    assert next(c for c in r.json()["edl"]["clips"] if c["id"] == hero["id"])["frame"] == 1.2
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "set_frame", "args": {"item_id": None, "frame": "16:9"}, "preview": False})
    assert all(abs(c["frame"] - 16 / 9) < 1e-6 for c in r.json()["edl"]["clips"])
    assert abs(client.get(f"/api/projects/{pid}").json()["options"]["frame"] - 16 / 9) < 1e-6
    r = client.post(f"/api/projects/{pid}/edl/op", json={"op": "set_frame", "args": {"item_id": None, "frame": "fill"}, "preview": False})
    assert all(c["frame"] is None for c in r.json()["edl"]["clips"])
    assert r.json()["changed"], "a swap must mark preview chunks dirty"
    v_before = client.get(f"/api/projects/{pid}/edl").json()["version"]
    u = client.post(f"/api/projects/{pid}/edl/undo").json()
    assert u["version"] == v_before - 1 and u["can_redo"]
    rd = client.post(f"/api/projects/{pid}/edl/redo").json()
    assert rd["version"] == v_before
    # edits persist across "reloads" (fresh GET)
    again = client.get(f"/api/projects/{pid}/edl").json()["edl"]
    assert again["text"][0]["text"] == "THE GIBBON"
    # regenerate keeps the locked title
    r = client.post(f"/api/projects/{pid}/plan", json={"preview": False})
    assert r.status_code == 200
    assert r.json()["edl"]["text"][0]["text"] == "THE GIBBON" and r.json()["edl"]["text"][0]["locked"]
    # export (preview quality to keep the test fast) with silent audio reports the offset
    r = client.post(f"/api/projects/{pid}/export", json={"quality": "preview"})
    assert r.status_code == 200
    run_pending()
    ex = client.get(f"/api/projects/{pid}/exports").json()
    assert ex, client.get(f"/api/projects/{pid}/jobs").json()[0]
    assert ex[0]["sound_offset"] is not None and Path(ex[0]["path"]).exists()
    assert "Sound offset" in ex[0]["caption"]
    assert Path(ex[0]["credits_path"]).exists()
    from wildcut.media import probe

    info = probe(ex[0]["path"])
    assert info.has_video and not info.has_audio and info.codec == "h264"


def test_visual_mode_and_chat_offline_order(client, assets, monkeypatch):
    from wildcut.claude import ClaudeClient, set_client_for_tests

    set_client_for_tests(ClaudeClient(api_key="", model="x"))
    try:
        p = _setup_project(client, assets, mode="visual", clips=("clip_a", "clip_b", "clip_c"))
        pid = p["id"]
        client.post(f"/api/projects/{pid}/analyze")
        run_pending()
        edl = client.get(f"/api/projects/{pid}/edl").json()["edl"]
        assert edl["mode"] == "visual" and edl["markers"]["drop"] is None
        r = client.post(f"/api/projects/{pid}/chat", json={"message": "clip 2 first, then clip 3, then clip 1"})
        assert r.status_code == 200
        run_pending()
        hist = client.get(f"/api/projects/{pid}/chat").json()
        assert hist[-1]["role"] == "assistant" and "Order set" in hist[-1]["content"]
        edl = client.get(f"/api/projects/{pid}/edl").json()["edl"]
        assert [c["label"] for c in edl["clips"]] == ["Clip 2", "Clip 3", "Clip 1"]
        assert all(c["locked_order"] for c in edl["clips"])
        # regenerate keeps the locked order
        r = client.post(f"/api/projects/{pid}/plan", json={"preview": False})
        assert [c["label"] for c in r.json()["edl"]["clips"]] == ["Clip 2", "Clip 3", "Clip 1"]
    finally:
        set_client_for_tests(None)


def test_showdown_flow_and_export_gate(client, assets):
    from wildcut.claude import ClaudeClient, set_client_for_tests

    set_client_for_tests(ClaudeClient(api_key="", model="x"))
    try:
        r = client.post("/api/projects", json={"name": "Fastest", "style": "showdown", "target_length": "30", "mode": "music"})
        pid = r.json()["id"]
        clips = {}
        for name, animal in (("clip_a", "cheetah"), ("clip_b", "lion"), ("clip_c", "peregrine falcon")):
            c = client.post(f"/api/projects/{pid}/clips", json={"path": str(Path(assets["dir"]) / assets["clips"][name]["path"])}).json()
            clips[animal] = c["id"]
        client.post(f"/api/projects/{pid}/song", json={"path": str(Path(assets["dir"]) / assets["track"]["path"])})
        r = client.put(f"/api/projects/{pid}/showdown", json={"stat": "top_speed", "animals": ["cheetah", "lion", "peregrine falcon"]})
        assert r.status_code == 200
        run_pending()  # song analysis + (offline) stats draft -> empty values flagged
        sd = client.get(f"/api/projects/{pid}/showdown").json()
        assert len(sd["stats"]) == 3 and set(sd["blockers"]) == {"cheetah", "lion", "peregrine falcon"}
        rows = [{"animal": "cheetah", "value": 110, "source_url": "https://en.wikipedia.org/wiki/Cheetah", "fact": "Fast.", "clip_id": clips["cheetah"]},
                {"animal": "lion", "value": 80, "source_url": "", "fact": "", "clip_id": clips["lion"]},
                {"animal": "peregrine falcon", "value": 389, "source_url": "https://en.wikipedia.org/wiki/Peregrine_falcon", "fact": "Dives.", "clip_id": clips["peregrine falcon"]}]
        r = client.put(f"/api/projects/{pid}/showdown/stats", json={"stats": rows})
        assert r.json()["blockers"] == ["lion"]
        client.post(f"/api/projects/{pid}/analyze", params={"then_plan": "true"})
        run_pending()
        edl = client.get(f"/api/projects/{pid}/edl").json()["edl"]
        assert edl["showdown"] and [a["name"] for a in edl["showdown"]["animals"]] == ["lion", "cheetah", "peregrine falcon"]
        r = client.post(f"/api/projects/{pid}/export", json={"quality": "preview"})
        assert r.status_code == 400 and "lion" in r.text
        rows[1]["confirmed"] = True
        client.put(f"/api/projects/{pid}/showdown/stats", json={"stats": rows})
        r = client.post(f"/api/projects/{pid}/export", json={"quality": "preview"})
        assert r.status_code == 200
        run_pending()
        ex = client.get(f"/api/projects/{pid}/exports").json()
        assert ex and Path(ex[0]["path"]).exists()
        assert Path(ex[0]["path"].replace(".mp4", "_stats.csv")).exists()
    finally:
        set_client_for_tests(None)


def test_stale_running_jobs_are_requeued(client):
    from datetime import timedelta

    from wildcut.db import Job, get_engine, now
    from wildcut.services.jobs import recover_stale

    with Session(get_engine()) as s:
        dead = Job(project_id="x", kind="analyze", status="running", updated_at=now() - timedelta(minutes=20))
        live = Job(project_id="x", kind="analyze", status="running", updated_at=now())
        s.add(dead)
        s.add(live)
        s.commit()
        assert recover_stale(s) == 1
        s.refresh(dead)
        s.refresh(live)
        assert dead.status == "queued" and live.status == "running"
        assert recover_stale(s, all_running=True) == 1     # worker startup takes the rest
        s.refresh(live)
        assert live.status == "queued"


def test_song_job_only_analyzes_the_song(client, assets, monkeypatch):
    """Attaching a song must not re-run clip analysis (Tim waited 8 minutes on 'Analyzing beats')."""
    pid = client.post("/api/projects", json={"name": "song only", "style": "phonk", "aspect": "9:16", "target_length": "15", "mode": "music"}).json()["id"]
    calls = {"clips": 0}

    import wildcut.services.analysis as an

    def no_clips(*a, **k):
        calls["clips"] += 1
        raise AssertionError("clip analysis must not run for a song-only job")

    monkeypatch.setattr(an, "analyze_project", no_clips)
    r = client.post(f"/api/projects/{pid}/song", json={"path": str(Path(assets["dir"]) / assets["track"]["path"])})
    assert r.status_code == 200, r.text
    run_pending()
    jobs = client.get(f"/api/projects/{pid}/jobs").json()
    song_job = next(j for j in jobs if j["payload"].get("only_song"))
    assert song_job["status"] == "done", song_job
    assert calls["clips"] == 0
    assert client.get(f"/api/projects/{pid}/beatgrid").status_code == 200


def test_config_and_stock_status(client):
    cfg = client.get("/api/config").json()
    assert {p["id"] for p in cfg["presets"]} >= {"phonk", "cinematic", "chase", "showdown"}
    assert "top_speed" in cfg["showdown_stats"]
    r = client.get("/api/stock/search", params={"q": "cheetah"})
    assert r.status_code in (200, 400)
    if r.status_code == 400:
        assert "API_KEY" in r.text


def test_brief_goes_to_the_director_after_the_first_plan(client, assets):
    """A brief written before the first edit is handed to the Director (when Claude is on), exactly once."""
    from wildcut.claude import ClaudeClient, set_client_for_tests
    from wildcut.db import get_engine
    from wildcut.services.jobs import claim_next, run_job

    set_client_for_tests(ClaudeClient(api_key="", model="x"))     # Claude off (the dev .env would enable it)
    pid = client.post("/api/projects", json={"name": "brief", "style": "phonk", "aspect": "9:16", "target_length": "15", "mode": "visual",
                                             "options": {"brief": "open on the snakes, title THE IGUANA"}}).json()["id"]
    client.post(f"/api/projects/{pid}/clips", json={"path": str(Path(assets["dir"]) / assets["clips"]["clip_a"]["path"])})
    # Claude off: no chat job, the preview runs straight away
    client.post(f"/api/projects/{pid}/analyze")
    run_pending()
    kinds = [j["kind"] for j in client.get(f"/api/projects/{pid}/jobs").json()]
    assert "chat" not in kinds and "preview" in kinds

    def run_one() -> None:   # run the next analyze job; brief chat jobs are left unrun (they need a real client)
        with Session(get_engine()) as s:
            while True:
                job = claim_next(s)
                assert job is not None
                if job.kind == "chat":
                    job.status = "cancelled"
                    s.add(job)
                    s.commit()
                    continue
                assert job.kind == "analyze"
                run_job(s, job)
                break

    set_client_for_tests(ClaudeClient(api_key="sk-ant-test", model="x"))
    try:
        client.post(f"/api/projects/{pid}/analyze")
        run_one()
        chats = [j for j in client.get(f"/api/projects/{pid}/jobs").json() if j["kind"] == "chat"]
        assert len(chats) == 1 and chats[0]["payload"]["brief"] and chats[0]["payload"]["message"].startswith("BRIEF:")
        assert "open on the snakes" in chats[0]["payload"]["message"]
        client.post(f"/api/projects/{pid}/analyze")   # a second plan must not queue the brief again
        run_one()
        chats = [j for j in client.get(f"/api/projects/{pid}/jobs").json() if j["kind"] == "chat"]
        assert len(chats) == 1
    finally:
        set_client_for_tests(None)


def test_failed_job_does_not_poison_the_session_or_restart_forever(client):
    """A handler that fails inside a flush must leave the job in error (not kill the worker), and a job that
    keeps dying is given up after MAX_RESTARTS instead of being re-queued forever."""
    from wildcut.db import Job, get_engine
    from wildcut.db import Moment as MomentRow
    from wildcut.services import jobs as jobsmod
    from wildcut.services.jobs import handler, recover_stale, run_job

    @handler("boom_test")
    def _boom(s, job, progress):
        s.add(MomentRow(id="dup", project_id="x", clip_id="c", in_t=0, out_t=1, peak_t=0.5))
        s.add(MomentRow(id="dup", project_id="x", clip_id="c", in_t=0, out_t=1, peak_t=0.5))
        s.commit()

    try:
        with Session(get_engine()) as s:
            job = Job(project_id="x", kind="boom_test", status="running")
            s.add(job)
            s.commit()
            run_job(s, job)                      # must not raise
            s.refresh(job)
            assert job.status == "error" and "UNIQUE" in job.error
            dead = Job(project_id="x", kind="analyze", status="running", payload={"_restarts": jobsmod.MAX_RESTARTS})
            s.add(dead)
            s.commit()
            assert recover_stale(s, all_running=True) == 1
            s.refresh(dead)
            assert dead.status == "error" and "gave up" in dead.error
    finally:
        jobsmod.HANDLERS.pop("boom_test", None)

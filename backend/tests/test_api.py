"""End-to-end API flow on the synthetic assets (jobs run in-process via run_pending)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def test_full_flow_music_phonk(client, assets):
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
    # preview was rendered by the analyze->plan->preview chain
    pv = client.get(f"/api/projects/{pid}/preview").json()
    assert pv["ready"] and pv["url"].startswith("/api/media?path=")
    media = client.get(pv["url"])
    assert media.status_code == 200 and media.headers["content-type"].startswith("video/mp4")
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


def test_config_and_stock_status(client):
    cfg = client.get("/api/config").json()
    assert {p["id"] for p in cfg["presets"]} >= {"phonk", "cinematic", "chase", "showdown"}
    assert "top_speed" in cfg["showdown_stats"]
    r = client.get("/api/stock/search", params={"q": "cheetah"})
    assert r.status_code in (200, 400)
    if r.status_code == 400:
        assert "API_KEY" in r.text

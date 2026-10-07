"""EDL (edit decision list): the single source of truth for an edit.

The planner writes it, the timeline UI and Director chat change it, the renderer reads it.
Every change is saved as a new version (see db.save_edl) so undo works everywhere.

Shape (all times in seconds; "timeline" means output time starting at 0):
{
  "version": 3, "project_id": "...", "style": "phonk", "aspect": "9:16", "fps": 30, "seed": 1,
  "mode": "music" | "visual", "duration": 30.0, "intensity": "med",
  "audio": {"export": "silent"|"mixed"|"original", "song_path": str|null,
            "song_window": {"start": s, "end": e}|null, "sound_offset": s|null, "window_locked": bool},
  "clips": [ {"id", "clip_id", "moment_id", "label", "src", "proxy", "in", "out", "start",
              "tl_duration", "speed": [keys]|null, "crop_path": {...}, "role", "locked_order",
              "locked_range", "anchor": null|"start"|"end", "peak": src_time|null, "enabled": true} ],
  "effects": [ {"id", "type", "t", "duration", "intensity", "enabled", "locked", "params": {}} ],
  "text":    [ {"id", "text", "t", "duration", "animation", "locked", "enabled", "style": {...}} ],
  "grade": {...}, "overlays": {...}, "transitions": {...},
  "markers": {"drop": tl|null, "hero_peak": tl|null, "beats": [...], "downbeats": [...], "bass_hits": [...]},
  "sections": [{"name", "start", "end"}],
  "notes": [str], "showdown": {...}|null, "chase": {...}|null
}
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from wildcut.planner.speed import source_at, timeline_duration

ASPECT_RATIOS = {"9:16": 9 / 16, "1:1": 1.0, "4:5": 0.8, "3:4": 0.75, "16:9": 16 / 9}
RENDER_WIDTHS = {"preview": 540, "full": 1080}


def new_item_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def output_size(aspect: str, width: int) -> tuple[int, int]:
    ratio = ASPECT_RATIOS.get(aspect, 9 / 16)
    h = int(round(width / ratio / 2)) * 2
    return width, h


def empty_edl(project_id: str, style: str, aspect: str, mode: str, seed: int = 1, fps: int = 30) -> dict:
    return {
        "version": 0, "project_id": project_id, "style": style, "aspect": aspect, "fps": fps, "seed": seed,
        "mode": mode, "duration": 0.0, "intensity": "med",
        "audio": {"export": "silent", "song_path": None, "song_window": None, "sound_offset": None, "window_locked": False},
        "clips": [], "effects": [], "text": [], "grade": {}, "overlays": {}, "transitions": {},
        "markers": {"drop": None, "hero_peak": None, "beats": [], "downbeats": [], "bass_hits": []},
        "sections": [], "notes": [], "showdown": None, "chase": None, "title": None,
    }


def clip_tl_duration(c: dict) -> float:
    return timeline_duration(c["in"], c["out"], c.get("speed"))


def relayout(edl: dict) -> dict:
    """Recompute timeline starts from clip order and durations. Returns the same dict."""
    t = 0.0
    for c in edl["clips"]:
        if not c.get("enabled", True):
            c["start"] = t
            c["tl_duration"] = 0.0
            continue
        c["tl_duration"] = round(clip_tl_duration(c), 4)
        c["start"] = round(t, 4)
        t += c["tl_duration"]
    edl["duration"] = round(t, 4)
    return edl


def clip_at(edl: dict, t: float) -> dict | None:
    for c in edl["clips"]:
        if c.get("enabled", True) and c["start"] - 1e-6 <= t < c["start"] + c["tl_duration"] - 1e-9:
            return c
    if edl["clips"] and abs(t - edl["duration"]) < 1e-6:
        return [c for c in edl["clips"] if c.get("enabled", True)][-1]
    return None


def source_time(c: dict, t: float) -> float:
    """Source time of clip c at timeline time t."""
    return source_at(c.get("speed"), c["in"], c["out"], t - c["start"])


def peak_timeline(c: dict) -> float | None:
    if c.get("peak") is None:
        return None
    from wildcut.planner.speed import timeline_between

    return round(c["start"] + timeline_between(c.get("speed"), c["in"], c["peak"]), 4)


def find_clip(edl: dict, item_id: str) -> dict | None:
    return next((c for c in edl["clips"] if c["id"] == item_id or c.get("clip_id") == item_id), None)


def find_item(edl: dict, item_id: str) -> tuple[str, dict] | None:
    for kind in ("clips", "effects", "text"):
        for it in edl[kind]:
            if it["id"] == item_id:
                return kind, it
    return None


def edl_hash(edl: dict) -> str:
    d = copy.deepcopy(edl)
    d.pop("version", None)
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def validate(edl: dict) -> list[str]:
    errors = []
    for c in edl.get("clips", []):
        if c["out"] <= c["in"]:
            errors.append(f"clip {c['id']}: out <= in")
        if c.get("peak") is not None and not (c["in"] - 1e-6 <= c["peak"] <= c["out"] + 1e-6):
            errors.append(f"clip {c['id']}: peak outside range")
    for e in edl.get("effects", []):
        if e["t"] < -1e-6 or e["t"] > edl.get("duration", 0) + 1e-6:
            errors.append(f"effect {e['id']} outside timeline")
    return errors


def lock_summary(edl: dict) -> dict[str, Any]:
    return {
        "clips": [c["id"] for c in edl["clips"] if c.get("locked_order") or c.get("locked_range")],
        "text": [t["id"] for t in edl["text"] if t.get("locked")],
        "effects": [e["id"] for e in edl["effects"] if e.get("locked")],
        "window": bool(edl["audio"].get("window_locked")),
    }

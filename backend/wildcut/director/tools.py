"""Director chat tools: read and change the EDL on Tim's behalf.

Clip references accept a number ("2"), a label ("Clip 2"), a clip id, an EDL item id, or free
text matched against species / descriptions ("the falcon one"). Ambiguous references return an
error listing the candidates so Claude can ask a short clarifying question.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from wildcut.analysis.moments import Moment
from wildcut.db import Clip, Project
from wildcut.media import extract_frames, frame_to_jpeg_b64
from wildcut.planner import edl as edlmod
from wildcut.planner.planner import ClipInfo
from wildcut.planner.speed import timeline_between
from wildcut.services import edl_ops
from wildcut.services.planning import (
    clip_infos,
    load_grid,
    move_cursor,
    plan_project,
    save_edl_version,
)


class ToolError(ValueError):
    pass


@dataclass
class Context:
    s: Session
    project: Project
    edl: dict
    clips: list[Clip]
    moments: list[Moment]
    changed: bool = False
    log: list[str] = field(default_factory=list)
    versions: list[int] = field(default_factory=list)

    @property
    def infos(self) -> dict[str, ClipInfo]:
        return {c.id: c for c in clip_infos(self.clips)}

    def commit(self, note: str) -> None:
        row = save_edl_version(self.s, self.project, self.edl, note=f"chat: {note}")
        self.versions.append(row.version)
        self.changed = True
        self.log.append(note)


# ---------------------------------------------------------------- references

def resolve_clip(ctx: Context, ref: Any) -> Clip:
    clips = ctx.clips
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        n = int(ref)
        for c in clips:
            if c.order == n:
                return c
        raise ToolError(f"there is no Clip {n}; clips are 1..{len(clips)}")
    r = str(ref).strip()
    m = re.match(r"^clip\s*(\d+)$", r, re.IGNORECASE)
    if m:
        return resolve_clip(ctx, int(m.group(1)))
    for c in clips:
        if c.id == r or c.label.lower() == r.lower():
            return c
    item = edlmod.find_clip(ctx.edl, r)
    if item is not None:
        c = next((c for c in clips if c.id == item["clip_id"]), None)
        if c:
            return c
    words = {w for w in re.findall(r"[a-z]+", r.lower()) if w not in {"the", "one", "clip", "a", "an", "with", "of"}}
    scored = []
    for c in clips:
        hay = f"{c.description} {(c.tags or {}).get('species', '')} {' '.join(m.caption_hint for m in ctx.moments if m.clip_id == c.id)}".lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, c))
    if not scored:
        raise ToolError(f"could not find a clip matching '{r}'. Clips: " + "; ".join(f"{c.label}: {c.description or 'no description'}" for c in clips))
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        cands = ", ".join(f"{c.label} ({c.description})" for _, c in scored[:3])
        raise ToolError(f"'{r}' is ambiguous: {cands}. Ask which one.")
    return scored[0][1]


def edl_item_for_clip(ctx: Context, clip: Clip, occurrence: int = 0) -> dict | None:
    items = [c for c in ctx.edl["clips"] if c.get("clip_id") == clip.id]
    return items[occurrence] if len(items) > occurrence else None


def best_moment(ctx: Context, clip: Clip) -> Moment:
    ms = [m for m in ctx.moments if m.clip_id == clip.id]
    if not ms:
        raise ToolError(f"{clip.label} has not been analyzed yet")
    return max(ms, key=lambda m: m.score)


def resolve_time(ctx: Context, t: Any) -> float:
    """Timeline seconds from: number | 'drop' | 'bass_hit:N' | 'downbeat:N' | 'beat:N' | 'end' | {clip, source_time}."""
    markers = ctx.edl.get("markers", {})
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, dict):
        clip = resolve_clip(ctx, t.get("clip"))
        s = float(t.get("source_time", 0.0))
        item = edl_item_for_clip(ctx, clip)
        if item is None:
            raise ToolError(f"{clip.label} is not in the edit yet; add it first (set_order or insert_clip)")
        if not (item["in"] - 1e-6 <= s <= item["out"] + 1e-6):
            ensure_source_in_range(ctx, item, s)
        return round(item["start"] + timeline_between(item.get("speed"), item["in"], s), 4)
    r = str(t).strip().lower()
    if r in ("drop", "the drop"):
        if markers.get("drop") is None:
            raise ToolError("this edit has no drop (visual-peaks mode or no song)")
        return float(markers["drop"])
    if r == "end":
        return float(ctx.edl["duration"])
    if r == "start":
        return 0.0
    m = re.match(r"^(bass_hit|downbeat|beat)s?[:\s#]*(\d+)$", r)
    if m:
        key = {"bass_hit": "bass_hits", "downbeat": "downbeats", "beat": "beats"}[m.group(1)]
        lst = markers.get(key) or []
        n = int(m.group(2))
        if not 1 <= n <= len(lst):
            raise ToolError(f"there are {len(lst)} {key} in the edit")
        return float(lst[n - 1])
    mm = re.match(r"^(\d+):(\d+(?:\.\d+)?)$", r)
    if mm:
        return int(mm.group(1)) * 60 + float(mm.group(2))
    try:
        return float(r)
    except ValueError as e:
        raise ToolError(f"cannot read time '{t}'") from e


def ensure_source_in_range(ctx: Context, item: dict, s: float) -> None:
    """Shift a clip's source range (keeping its length) so source time s is inside it."""
    length = item["out"] - item["in"]
    info = ctx.infos[item["clip_id"]]
    if s < item["in"]:
        new_in = max(0.0, s - 0.25)
    else:
        new_in = min(info.duration - length, s - length + 0.35)
    new_in = max(0.0, new_in)
    edl_ops.set_clip_range(ctx.edl, item["id"], new_in, new_in + length)


# ---------------------------------------------------------------- summaries

def summarize_edit(ctx: Context) -> dict:
    e = ctx.edl
    clips = []
    for c in e["clips"]:
        clips.append({"item_id": c["id"], "clip": c.get("label"), "clip_id": c.get("clip_id"), "role": c["role"],
                      "timeline": [round(c["start"], 2), round(c["start"] + c["tl_duration"], 2)],
                      "source": [round(c["in"], 2), round(c["out"], 2)], "speed_ramp": bool(c.get("speed")),
                      "peak_source": c.get("peak"), "locked": bool(c.get("locked_order") or c.get("locked_range")),
                      "kind": c.get("kind", "video")})
    fx: dict[str, int] = {}
    for f in e["effects"]:
        fx[f["type"]] = fx.get(f["type"], 0) + 1
    return {"version": e.get("version"), "style": e["style"], "aspect": e["aspect"], "mode": e["mode"], "duration": round(e["duration"], 2),
            "title": e.get("title"), "clips": clips,
            "text": [{"id": t["id"], "text": t["text"], "t": t["t"], "duration": t["duration"], "locked": t.get("locked")} for t in e["text"]],
            "effects_count": fx, "markers": {"drop": e["markers"].get("drop"), "hero_peak": e["markers"].get("hero_peak"),
                                             "n_bass_hits": len(e["markers"].get("bass_hits", [])), "n_downbeats": len(e["markers"].get("downbeats", []))},
            "song_window": e["audio"].get("song_window"), "window_locked": e["audio"].get("window_locked"), "notes": e.get("notes", [])}


def summarize_clips(ctx: Context) -> list[dict]:
    out = []
    for c in ctx.clips:
        ms = sorted((m for m in ctx.moments if m.clip_id == c.id), key=lambda m: -m.score)[:3]
        out.append({"clip": c.label, "number": c.order, "clip_id": c.id, "duration": round(c.duration, 2),
                    "description": c.description, "species": (c.tags or {}).get("species", ""), "analyzed": c.analyzed,
                    "in_edit": any(x.get("clip_id") == c.id for x in ctx.edl["clips"]),
                    "top_moments": [{"id": m.id, "peak": m.peak_t, "action": m.action, "what": m.caption_hint, "score": m.score} for m in ms]})
    return out


# ---------------------------------------------------------------- tool implementations

def t_list_clips(ctx: Context, **_) -> dict:
    return {"clips": summarize_clips(ctx)}


def t_get_moments(ctx: Context, clip: Any, **_) -> dict:
    c = resolve_clip(ctx, clip)
    ms = sorted((m for m in ctx.moments if m.clip_id == c.id), key=lambda m: m.peak_t)
    return {"clip": c.label, "moments": [{"id": m.id, "in": m.in_t, "out": m.out_t, "peak": m.peak_t, "action": m.action,
                                           "what": m.caption_hint, "intensity": m.intensity, "score": m.score} for m in ms]}


def t_get_edit(ctx: Context, **_) -> dict:
    return summarize_edit(ctx)


def t_look_at(ctx: Context, clip: Any, start: float, end: float, fps: float = 3.0, **_) -> list[dict]:
    c = resolve_clip(ctx, clip)
    start, end = max(0.0, float(start)), min(c.duration, float(end))
    if end <= start:
        raise ToolError("end must be after start")
    fps = max(2.0, min(4.0, float(fps)))
    n = min(12, max(2, int((end - start) * fps) + 1))
    times = [start + i * (end - start) / (n - 1) for i in range(n)]
    frames = extract_frames(c.proxy_path or c.path, times, width=448)
    content: list[dict] = [{"type": "text", "text": f"{c.label} frames at source times: " + ", ".join(f"{t:.2f}s" for t in times[:len(frames)])}]
    for t, f in zip(times, frames):
        content.append({"type": "text", "text": f"t={t:.2f}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_to_jpeg_b64(f, quality=70)}})
    return content


def t_set_order(ctx: Context, clips: list[Any], **_) -> dict:
    ids = []
    for ref in clips:
        c = resolve_clip(ctx, ref)
        item = edl_item_for_clip(ctx, c)
        if item is None:
            m = best_moment(ctx, c)
            item = edl_ops.insert_clip(ctx.edl, m, ctx.infos[c.id], None, duration=min(2.0, c.duration), role="build")
        ids.append(item["id"])
    note = edl_ops.set_order(ctx.edl, ids)
    # clips not named are dropped from the edit when the user gives a full order
    named = set(ids)
    others = [c for c in ctx.edl["clips"] if c["id"] not in named]
    if others and len(ids) >= 2:
        ctx.edl["clips"] = [c for c in ctx.edl["clips"] if c["id"] in named]
        edlmod.relayout(ctx.edl)
        note += f"; removed {len(others)} unnamed clip(s)"
    ctx.commit(note)
    # re-fit the locked order onto the beat grid (hero on the drop, title placed); locks are honored
    row = plan_project(ctx.s, ctx.project, keep_locks=True, note="chat: refit order")
    ctx.edl = row.json
    ctx.versions.append(row.version)
    return {"ok": True, "note": note, "edit": summarize_edit(ctx)}


def t_insert_clip(ctx: Context, clip: Any, position: int | None = None, duration: float | None = None, source_start: float | None = None,
                  source_end: float | None = None, **_) -> dict:
    c = resolve_clip(ctx, clip)
    m = best_moment(ctx, c)
    d = float(duration) if duration else min(2.0, c.duration)
    entry = edl_ops.insert_clip(ctx.edl, m, ctx.infos[c.id], position, d)
    if source_start is not None or source_end is not None:
        edl_ops.set_clip_range(ctx.edl, entry["id"], source_start, source_end)
    ctx.commit(f"inserted {c.label}")
    return {"ok": True, "item_id": entry["id"], "edit": summarize_edit(ctx)}


def t_remove_clip(ctx: Context, clip: Any, **_) -> dict:
    c = resolve_clip(ctx, clip)
    item = edl_item_for_clip(ctx, c)
    if item is None:
        raise ToolError(f"{c.label} is not in the edit")
    note = edl_ops.remove_clip(ctx.edl, item["id"])
    ctx.commit(note)
    return {"ok": True, "note": note}


def t_set_clip_range(ctx: Context, clip: Any, source_start: float | None = None, source_end: float | None = None, **_) -> dict:
    c = resolve_clip(ctx, clip)
    item = edl_item_for_clip(ctx, c)
    if item is None:
        m = best_moment(ctx, c)
        item = edl_ops.insert_clip(ctx.edl, m, ctx.infos[c.id], None, duration=2.0)
    note = edl_ops.set_clip_range(ctx.edl, item["id"], source_start, source_end)
    ctx.commit(note)
    return {"ok": True, "note": note, "edit": summarize_edit(ctx)}


def t_set_title(ctx: Context, text: str | None = None, time: Any = None, duration: float | None = None, **_) -> dict:
    anchor = None
    if isinstance(time, dict):
        clip = resolve_clip(ctx, time.get("clip"))
        t = resolve_time(ctx, time)   # also pulls the source time into the clip's range
        item = edl_item_for_clip(ctx, clip)
        anchor = {"item_id": item["id"], "clip_id": clip.id, "source_time": float(time.get("source_time", 0.0))} if item else None
    else:
        t = resolve_time(ctx, time) if time is not None else None
    note = edl_ops.set_title(ctx.edl, text=text, t=t, duration=duration, anchor=anchor)
    ctx.commit(note)
    if anchor and ctx.project.mode == "music":
        # an anchored title names the moment that matters: re-fit so the drop lands on it (locks are honored)
        row = plan_project(ctx.s, ctx.project, keep_locks=True, note="chat: refit to anchored title")
        ctx.edl = row.json
        ctx.versions.append(row.version)
        note += "; drop moved to that moment"
    return {"ok": True, "note": note, "title": next((x for x in ctx.edl["text"] if x.get("kind") != "marker"), None)}


def t_add_effect(ctx: Context, type: str, time: Any, intensity: str | None = None, **_) -> dict:
    if type not in ("shake", "flash", "chromatic", "zoom_punch", "glitch", "fade_black", "push_in", "motion_blur"):
        raise ToolError("unknown effect type")
    t = resolve_time(ctx, time)
    e = edl_ops.add_effect(ctx.edl, type, t, intensity)
    ctx.commit(f"added {type} at {t:.2f}s")
    return {"ok": True, "effect_id": e["id"], "t": e["t"]}


def t_remove_effect(ctx: Context, effect_id: str | None = None, type: str | None = None, near_time: Any = None, all_of_type: bool = False, **_) -> dict:
    if effect_id:
        note = edl_ops.delete_item(ctx.edl, effect_id)
        ctx.commit(note)
        return {"ok": True, "note": note}
    if type and all_of_type:
        n = len([e for e in ctx.edl["effects"] if e["type"] == type])
        ctx.edl["effects"] = [e for e in ctx.edl["effects"] if e["type"] != type]
        ctx.commit(f"removed all {n} {type} effects")
        return {"ok": True, "removed": n}
    if type and near_time is not None:
        t = resolve_time(ctx, near_time)
        cands = [e for e in ctx.edl["effects"] if e["type"] == type]
        if not cands:
            raise ToolError(f"no {type} effects")
        e = min(cands, key=lambda x: abs(x["t"] - t))
        note = edl_ops.delete_item(ctx.edl, e["id"])
        ctx.commit(note)
        return {"ok": True, "note": note}
    raise ToolError("give effect_id, or type with near_time or all_of_type")


def t_toggle_effect(ctx: Context, effect_id: str, enabled: bool, **_) -> dict:
    note = edl_ops.toggle_effect(ctx.edl, effect_id, enabled)
    ctx.commit(note)
    return {"ok": True, "note": note}


def t_set_intensity(ctx: Context, intensity: str, effect_id: str | None = None, **_) -> dict:
    note = edl_ops.set_effect_intensity(ctx.edl, effect_id, intensity) if effect_id else edl_ops.set_all_intensity(ctx.edl, intensity)
    ctx.commit(note)
    return {"ok": True, "note": note}


def t_set_speed_ramp(ctx: Context, clip: Any, slow_rate: float | None = 0.4, source_time: float | None = None, **_) -> dict:
    c = resolve_clip(ctx, clip)
    item = edl_item_for_clip(ctx, c)
    if item is None:
        raise ToolError(f"{c.label} is not in the edit")
    if source_time is not None and not (item["in"] <= source_time <= item["out"]):
        ensure_source_in_range(ctx, item, float(source_time))
    note = edl_ops.set_speed_ramp(ctx.edl, item["id"], slow_rate, source_time)
    ctx.commit(note)
    return {"ok": True, "note": note}


def t_set_focus(ctx: Context, subject: str, action: str | None = None, **_) -> dict:
    """What the edit is about. Re-plans everything unlocked: the subject's key action becomes the hero on the drop,
    shots without the subject only build tension, the title becomes THE <SUBJECT>."""
    subject = (subject or "").strip().lower()
    if not subject:
        raise ToolError("subject is required")
    opts = dict(ctx.project.options or {})
    opts["focus"] = {"subject": subject, "action": (action or "").strip().lower()}
    opts.pop("title", None)
    ctx.project.options = opts
    ctx.s.add(ctx.project)
    ctx.s.commit()
    row = plan_project(ctx.s, ctx.project, keep_locks=False, note=f"chat: focus {subject}")
    ctx.edl = row.json
    ctx.versions.append(row.version)
    ctx.changed = True
    hero = next((c for c in ctx.edl["clips"] if c.get("role") == "hero"), None)
    shown = sum(1 for c in ctx.edl["clips"] if subject in f"{c.get('species', '')} {c.get('caption_hint', '')}".lower())
    return {"ok": True, "note": f"the edit is now about the {subject}" + (f" ({action})" if action else ""),
            "hero": {"clip": hero.get("label"), "source_time": hero.get("peak"), "caption": hero.get("caption_hint")} if hero else None,
            "clips_showing_subject": shown, "edit": summarize_edit(ctx)}


def t_set_hero(ctx: Context, clip: Any, source_time: float, **_) -> dict:
    """Make the moment at a source time the hero (it lands on the drop); creates a moment there if none was detected."""
    from wildcut.analysis.moments import Moment as MomentData
    from wildcut.db import Moment as MomentRow

    c = resolve_clip(ctx, clip)
    t = float(source_time)
    if not (0 <= t <= c.duration + 1e-6):
        raise ToolError(f"{c.label} is only {c.duration:.1f}s long")
    near = [m for m in ctx.moments if m.clip_id == c.id and abs(m.peak_t - t) <= 1.5]
    if near:
        m = min(near, key=lambda m: abs(m.peak_t - t))
    else:
        in_t, out_t = max(0.0, t - 1.5), min(c.duration, t + 1.5)
        m = MomentData(id=uuid.uuid4().hex[:12], clip_id=c.id, in_t=in_t, out_t=out_t, peak_t=t, motion_score=0.8, score=0.9,
                       action="tim's pick", caption_hint=f"Tim's moment at {t:.1f}s", kind="user", shot_start=in_t, shot_end=out_t)
        ctx.s.add(MomentRow(id=m.id, project_id=ctx.project.id, clip_id=c.id, in_t=in_t, out_t=out_t, peak_t=t, motion_score=0.8,
                            caption_hint=m.caption_hint, action=m.action, kind="user", score=0.9, starred=True))
        ctx.s.commit()
        ctx.moments.append(m)
    hero = next((x for x in ctx.edl["clips"] if x.get("role") == "hero"), None)
    if hero is None:
        raise ToolError("this edit has no hero slot yet; run plan_auto first")
    note = edl_ops.swap_moment(ctx.edl, hero["id"], m, ctx.infos[c.id])
    hero["anchor"] = "drop"
    opts = dict(ctx.project.options or {})
    opts["starred_moments"] = sorted(set(opts.get("starred_moments", [])) | {m.id})
    ctx.project.options = opts
    ctx.s.add(ctx.project)
    ctx.commit(f"hero: {c.label} at {t:.1f}s")
    return {"ok": True, "note": note, "hero_item": hero["id"], "edit": summarize_edit(ctx)}


def t_set_frame(ctx: Context, frame: Any, clip: Any = None, **_) -> dict:
    if clip in (None, "", "all"):
        note = edl_ops.set_frame(ctx.edl, None, frame)
        opts = dict(ctx.project.options or {})
        opts["frame"] = edl_ops.parse_frame_arg(frame)
        ctx.project.options = opts
        ctx.s.add(ctx.project)
    else:
        c = resolve_clip(ctx, clip)
        item = edl_item_for_clip(ctx, c)
        if item is None:
            raise ToolError(f"{c.label} is not in the edit")
        note = edl_ops.set_frame(ctx.edl, item["id"], frame)
    ctx.commit(note)
    return {"ok": True, "note": note}


def t_set_style(ctx: Context, style: str, aspect: str | None = None, **_) -> dict:
    from wildcut.planner.presets import load_preset

    try:
        preset = load_preset(style)
    except FileNotFoundError as e:
        raise ToolError(str(e)) from e
    ctx.project.style = preset["id"]
    if aspect:
        if aspect not in edlmod.ASPECT_RATIOS:
            raise ToolError("aspect must be one of 9:16, 1:1, 4:5, 3:4")
        ctx.project.aspect = aspect
    elif preset.get("default_aspect"):
        ctx.project.aspect = preset["default_aspect"]
    ctx.s.add(ctx.project)
    ctx.s.commit()
    row = plan_project(ctx.s, ctx.project, keep_locks=True, note=f"chat: style {style}")
    ctx.edl = row.json
    ctx.versions.append(row.version)
    ctx.changed = True
    return {"ok": True, "note": f"style {preset['id']} ({ctx.project.aspect})", "edit": summarize_edit(ctx)}


def t_set_song_window(ctx: Context, start: float, end: float, **_) -> dict:
    grid = load_grid(ctx.s, ctx.project.id)
    if grid is None:
        raise ToolError("no song analyzed for this project")
    start, end = max(0.0, float(start)), min(grid.duration, float(end))
    if end - start < 5:
        raise ToolError("window must be at least 5 s")
    ctx.project.song_window = {"start": round(start, 3), "end": round(end, 3)}
    edl_ops.set_song_window(ctx.edl, start, end)
    ctx.s.add(ctx.project)
    ctx.s.commit()
    save_edl_version(ctx.s, ctx.project, ctx.edl, note="chat: song window")
    row = plan_project(ctx.s, ctx.project, keep_locks=True, note="chat: song window replan")
    ctx.edl = row.json
    ctx.versions.append(row.version)
    ctx.changed = True
    return {"ok": True, "note": f"song window {start:.1f}-{end:.1f}s", "edit": summarize_edit(ctx)}


def t_plan_auto(ctx: Context, scope: str = "fill", new_seed: bool = False, **_) -> dict:
    seed = (ctx.project.seed + 1) if new_seed else None
    if scope == "text":
        note = edl_ops.rebuild_text(ctx.edl)
        ctx.commit(note)
        return {"ok": True, "note": note}
    row = plan_project(ctx.s, ctx.project, seed=seed, keep_locks=(scope != "all_unlock"), note=f"chat: plan_auto {scope}")
    ctx.edl = row.json
    ctx.versions.append(row.version)
    ctx.changed = True
    return {"ok": True, "note": "auto-filled the unlocked parts", "edit": summarize_edit(ctx)}


def t_render_preview(ctx: Context, **_) -> dict:
    from wildcut.services.jobs import enqueue

    enqueue(ctx.s, ctx.project.id, "preview", {})
    return {"ok": True, "note": "preview render queued"}


def t_undo(ctx: Context, steps: int = 1, **_) -> dict:
    row = move_cursor(ctx.s, ctx.project, -int(steps))
    if row is None:
        raise ToolError("nothing to undo")
    ctx.edl = row.json
    ctx.changed = True
    ctx.log.append(f"undo -> v{row.version}")
    return {"ok": True, "version": row.version, "edit": summarize_edit(ctx)}


def t_set_lock(ctx: Context, item_id: str, locked: bool = True, **_) -> dict:
    note = edl_ops.set_lock(ctx.edl, item_id, locked)
    ctx.commit(note)
    return {"ok": True, "note": note}


TOOL_IMPLS = {
    "list_clips": t_list_clips, "get_moments": t_get_moments, "get_edit": t_get_edit, "look_at": t_look_at,
    "set_order": t_set_order, "insert_clip": t_insert_clip, "remove_clip": t_remove_clip, "set_clip_range": t_set_clip_range,
    "set_title": t_set_title, "add_effect": t_add_effect, "remove_effect": t_remove_effect, "toggle_effect": t_toggle_effect,
    "set_intensity": t_set_intensity, "set_speed_ramp": t_set_speed_ramp, "set_style": t_set_style,
    "set_song_window": t_set_song_window, "plan_auto": t_plan_auto, "render_preview": t_render_preview, "undo": t_undo,
    "set_lock": t_set_lock,
    "set_frame": t_set_frame,
    "set_focus": t_set_focus,
    "set_hero": t_set_hero,
}

_CLIP = {"type": ["string", "integer"], "description": "clip number (2), label ('Clip 2'), clip id, or a short description ('the falcon one')"}
_TIME = {"type": ["number", "string", "object"], "description": "timeline seconds, 'drop', 'bass_hit:N', 'downbeat:N', 'beat:N', 'end', or {clip, source_time} for a moment inside a clip"}

TOOLS: list[dict] = [
    {"name": "list_clips", "description": "List the project's clips with labels, descriptions, species, durations, top moments, and whether each is in the edit.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_moments", "description": "Detected action moments of one clip (source times, actions, scores).", "input_schema": {"type": "object", "properties": {"clip": _CLIP}, "required": ["clip"]}},
    {"name": "get_edit", "description": "Current edit: ordered timeline clips with item ids, source ranges, roles, locks; title; effect counts; drop/beat markers; song window.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "look_at", "description": "Sample frames (2-4 fps, max 12) from a clip's source time range to find a moment Tim describes, e.g. 'when it lets go of the branch'. Returns images with their source times.", "input_schema": {"type": "object", "properties": {"clip": _CLIP, "start": {"type": "number"}, "end": {"type": "number"}, "fps": {"type": "number"}}, "required": ["clip", "start", "end"]}},
    {"name": "set_order", "description": "Set the clip order for the whole edit. Clips not yet in the edit are added at their best moment. When two or more clips are named, clips not named are removed. Locks the order.", "input_schema": {"type": "object", "properties": {"clips": {"type": "array", "items": _CLIP}}, "required": ["clips"]}},
    {"name": "insert_clip", "description": "Insert a clip (at its best moment) at a position in the edit (0 = first; omit = end).", "input_schema": {"type": "object", "properties": {"clip": _CLIP, "position": {"type": "integer"}, "duration": {"type": "number"}, "source_start": {"type": "number"}, "source_end": {"type": "number"}}, "required": ["clip"]}},
    {"name": "remove_clip", "description": "Remove a clip from the edit.", "input_schema": {"type": "object", "properties": {"clip": _CLIP}, "required": ["clip"]}},
    {"name": "set_clip_range", "description": "Trim a clip's source range (seconds in the source file). Locks the range.", "input_schema": {"type": "object", "properties": {"clip": _CLIP, "source_start": {"type": "number"}, "source_end": {"type": "number"}}, "required": ["clip"]}},
    {"name": "set_title", "description": "Set the title text and/or when it appears. The only text in a single-animal edit is the animal's name, e.g. 'THE GIBBON'. Locks the title.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "time": _TIME, "duration": {"type": "number"}}}},
    {"name": "add_effect", "description": "Add an effect at a time.", "input_schema": {"type": "object", "properties": {"type": {"type": "string", "enum": ["shake", "flash", "chromatic", "zoom_punch", "glitch", "fade_black", "push_in", "motion_blur"]}, "time": _TIME, "intensity": {"type": "string", "enum": ["low", "med", "high"]}}, "required": ["type", "time"]}},
    {"name": "remove_effect", "description": "Remove an effect by id, or the one of a type nearest a time, or all of a type.", "input_schema": {"type": "object", "properties": {"effect_id": {"type": "string"}, "type": {"type": "string"}, "near_time": _TIME, "all_of_type": {"type": "boolean"}}}},
    {"name": "toggle_effect", "description": "Enable or disable one effect.", "input_schema": {"type": "object", "properties": {"effect_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["effect_id", "enabled"]}},
    {"name": "set_intensity", "description": "Set effect intensity (low/med/high) for one effect or all effects.", "input_schema": {"type": "object", "properties": {"intensity": {"type": "string", "enum": ["low", "med", "high"]}, "effect_id": {"type": "string"}}, "required": ["intensity"]}},
    {"name": "set_speed_ramp", "description": "Put a slow-motion ramp on a clip around a source time (slow_rate 0.3-0.7), or remove it (slow_rate null/1).", "input_schema": {"type": "object", "properties": {"clip": _CLIP, "slow_rate": {"type": ["number", "null"]}, "source_time": {"type": "number"}}, "required": ["clip"]}},
    {"name": "set_focus", "description": "Declare what the edit is about and rebuild it around that: subject (the animal or thing, e.g. 'iguana') and optionally its key action (e.g. 'escape'). Moments showing the subject rank first, its key action becomes the hero on the drop, other animals only build tension, the title becomes THE <SUBJECT>. Use whenever Tim says what the edit is about or that it is built around the wrong animal; it replaces unpinned clips.", "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "action": {"type": "string"}}, "required": ["subject"]}},
    {"name": "set_hero", "description": "Make the moment at a source time of a clip the hero: it is placed so its peak lands on the drop (or the visual payoff) with the slow-mo ramp, and it is pinned. Use for the moment Tim calls the biggest / the payoff / 'the drop should hit when...'. Creates the moment if analysis did not detect one there.", "input_schema": {"type": "object", "properties": {"clip": _CLIP, "source_time": {"type": "number", "description": "seconds into the clip's source file"}}, "required": ["clip", "source_time"]}},
    {"name": "set_frame", "description": "How a clip sits in the vertical canvas. 'fill' crops it to fill the whole frame (default). An aspect such as '1.2:1', '4:3' or '16:9' shows the clip as a centered box of that shape at full width with black above and below, which suits wide shots that crop badly to 9:16. Omit clip (or 'all') to apply to every clip and make it the project default.", "input_schema": {"type": "object", "properties": {"frame": {"type": "string", "description": "'fill', '1.2:1', '1:1', '4:3', '16:9', or a w/h number as text"}, "clip": _CLIP}, "required": ["frame"]}},
    {"name": "set_style", "description": "Switch the style preset (phonk, cinematic, chase) and optionally the aspect; re-plans the unlocked parts.", "input_schema": {"type": "object", "properties": {"style": {"type": "string"}, "aspect": {"type": "string", "enum": ["9:16", "1:1", "4:5", "3:4"]}}, "required": ["style"]}},
    {"name": "set_song_window", "description": "Choose which part of the song the edit uses (seconds in the song file); re-plans the unlocked parts.", "input_schema": {"type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}}, "required": ["start", "end"]}},
    {"name": "plan_auto", "description": "Auto-fill whatever Tim did not specify. scope: 'fill' keeps every locked choice and re-plans the rest; 'text' only regenerates the title; 'all_unlock' ignores locks.", "input_schema": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["fill", "text", "all_unlock"]}, "new_seed": {"type": "boolean"}}}},
    {"name": "render_preview", "description": "Queue a preview render of the current edit.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "undo", "description": "Undo the last change(s) to the edit.", "input_schema": {"type": "object", "properties": {"steps": {"type": "integer"}}}},
    {"name": "set_lock", "description": "Pin or unpin an edit item (clip item id, text id, effect id) so auto-planning keeps or may change it.", "input_schema": {"type": "object", "properties": {"item_id": {"type": "string"}, "locked": {"type": "boolean"}}, "required": ["item_id"]}},
]


def run_tool(ctx: Context, name: str, args: dict) -> tuple[str | list[dict], bool]:
    """Returns (tool result content, is_error)."""
    fn = TOOL_IMPLS.get(name)
    if fn is None:
        return f"unknown tool {name}", True
    try:
        res = fn(ctx, **args)
    except (ToolError, edl_ops.EdlOpError, ValueError, KeyError, TypeError) as e:
        return f"error: {e}", True
    if isinstance(res, list):
        return res, False
    return json.dumps(res, default=str), False

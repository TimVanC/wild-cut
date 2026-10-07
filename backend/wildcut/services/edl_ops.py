"""Small EDL operations shared by the timeline UI and the Director chat.

Every operation takes an EDL dict, mutates it, relayouts, and returns a one-line note. Manual
choices are locked so Regenerate keeps them (the timeline shows a pin).
"""
from __future__ import annotations

from wildcut.analysis.moments import Moment
from wildcut.analysis.tracking import crop_size
from wildcut.planner import edl as edlmod
from wildcut.planner.planner import ClipInfo
from wildcut.planner.presets import load_preset, pick
from wildcut.planner.speed import hero_ramp, timeline_between, timeline_duration


class EdlOpError(ValueError):
    pass


def _clip(edl: dict, item_id: str) -> dict:
    c = edlmod.find_clip(edl, item_id)
    if c is None:
        raise EdlOpError(f"no clip {item_id} in the edit")
    return c


def _crop(m: Moment, aspect: str, clip: ClipInfo, in_t: float, out_t: float) -> dict:
    cp = m.crop_paths.get(aspect)
    if cp:
        return cp
    cw, ch = crop_size(clip.aspect, edlmod.ASPECT_RATIOS[aspect])
    return {"aspect": edlmod.ASPECT_RATIOS[aspect], "cw": cw, "ch": ch,
            "keys": [{"t": in_t, "cx": 0.5, "cy": 0.5}, {"t": out_t, "cx": 0.5, "cy": 0.5}]}


def swap_moment(edl: dict, item_id: str, m: Moment, clip: ClipInfo) -> str:
    """Replace the moment behind a timeline clip, keeping its timeline length and speed curve."""
    c = _clip(edl, item_id)
    tl = c["tl_duration"] or timeline_duration(c["in"], c["out"], c.get("speed"))
    old_peak_rel = ((c["peak"] - c["in"]) / max(1e-6, c["out"] - c["in"])) if c.get("peak") is not None else 0.7
    speed = None
    if c.get("speed"):
        slow = min(k["rate"] for k in c["speed"])
        speed = hero_ramp(m.peak_t, slow_rate=slow)
        # keep the same lead-in on the timeline so a hero stays on the drop
        lead = timeline_between(c.get("speed"), c["in"], c["peak"]) if c.get("peak") is not None else tl * old_peak_rel
        lo, hi = max(0.0, m.shot_start), m.peak_t - 0.05
        in_t = lo
        for _ in range(40):
            mid = (lo + hi) / 2
            if timeline_between(speed, mid, m.peak_t) > lead:
                lo = mid
            else:
                hi = mid
            in_t = (lo + hi) / 2
        out_t = m.peak_t + 0.05
        # extend out so the timeline duration matches
        for _ in range(40):
            if timeline_duration(in_t, out_t, speed) >= tl or out_t >= min(clip.duration, m.shot_end or clip.duration):
                break
            out_t += 0.05
    else:
        d = tl
        in_t = max(max(0.0, m.shot_start), m.peak_t - old_peak_rel * d)
        out_t = min(clip.duration, in_t + d)
        in_t = max(0.0, out_t - d)
    c.update({"clip_id": m.clip_id, "moment_id": m.id, "label": clip.label, "src": clip.path, "proxy": clip.proxy,
              "in": round(in_t, 4), "out": round(out_t, 4), "speed": speed, "crop_path": _crop(m, edl["aspect"], clip, in_t, out_t),
              "peak": round(min(max(m.peak_t, in_t), out_t), 4), "species": m.species, "action": m.action,
              "caption_hint": m.caption_hint, "locked_order": True, "locked_range": True})
    edlmod.relayout(edl)
    return f"Swapped {c['label']} to {m.caption_hint or m.action or 'another moment'} at {m.peak_t:.1f}s"


def set_clip_range(edl: dict, item_id: str, in_t: float | None = None, out_t: float | None = None, lock: bool = True) -> str:
    c = _clip(edl, item_id)
    if in_t is not None:
        c["in"] = round(max(0.0, float(in_t)), 4)
    if out_t is not None:
        c["out"] = round(float(out_t), 4)
    if c["out"] - c["in"] < 0.15:
        raise EdlOpError("clip range must be at least 0.15 s")
    if c.get("peak") is not None:
        c["peak"] = round(min(max(c["peak"], c["in"]), c["out"]), 4)
    if lock:
        c["locked_range"] = True
    edlmod.relayout(edl)
    return f"Trimmed {c['label']} to {c['in']:.2f}-{c['out']:.2f}s"


def set_order(edl: dict, ids: list[str], lock: bool = True) -> str:
    """Reorder timeline clips; ids may be EDL item ids or source clip ids (first match)."""
    chosen: list[dict] = []
    for i in ids:
        c = edlmod.find_clip(edl, i)
        if c is None:
            raise EdlOpError(f"unknown clip {i}")
        if c in chosen:
            continue
        chosen.append(c)
    rest = [c for c in edl["clips"] if c not in chosen]
    for c in chosen:
        if lock:
            c["locked_order"] = True
    edl["clips"] = chosen + rest
    edlmod.relayout(edl)
    return "Order set: " + ", ".join(c["label"] for c in chosen)


def insert_clip(edl: dict, m: Moment, clip: ClipInfo, position: int | None, duration: float, role: str = "build",
                lock: bool = True) -> dict:
    in_t = max(max(0.0, m.shot_start), m.peak_t - 0.7 * duration)
    out_t = min(clip.duration, in_t + duration)
    entry = {"id": edlmod.new_item_id("c"), "clip_id": m.clip_id, "moment_id": m.id, "label": clip.label, "src": clip.path,
             "proxy": clip.proxy, "in": round(in_t, 4), "out": round(out_t, 4), "start": 0.0, "tl_duration": 0.0, "speed": None,
             "crop_path": _crop(m, edl["aspect"], clip, in_t, out_t), "role": role, "locked_order": lock, "locked_range": lock,
             "anchor": None, "peak": round(min(max(m.peak_t, in_t), out_t), 4), "enabled": True, "species": m.species,
             "action": m.action, "caption_hint": m.caption_hint}
    if position is None or position >= len(edl["clips"]):
        edl["clips"].append(entry)
    else:
        edl["clips"].insert(max(0, position), entry)
    edlmod.relayout(edl)
    return entry


def remove_clip(edl: dict, item_id: str) -> str:
    c = _clip(edl, item_id)
    edl["clips"] = [x for x in edl["clips"] if x is not c]
    edlmod.relayout(edl)
    return f"Removed {c['label']}"


def set_title(edl: dict, text: str | None = None, t: float | None = None, duration: float | None = None,
              lock: bool = True, preset_id: str | None = None, anchor: dict | None = None) -> str:
    titles = [x for x in edl["text"] if x.get("kind") != "marker"]
    if not titles:
        preset = load_preset(preset_id or edl["style"])
        style = dict(preset.get("text", {}))
        titles = [{"id": edlmod.new_item_id("t"), "text": text or edl.get("title") or "THE ANIMAL", "t": 0.0,
                   "duration": style.get("hold", 0.9), "animation": style.get("animation", "flash_in"), "locked": False,
                   "enabled": True, "style": style}]
        edl["text"].append(titles[0])
    item = titles[0]
    if text is not None:
        item["text"] = text
        edl["title"] = text
    if t is not None:
        item["t"] = round(max(0.0, min(float(t), max(0.0, edl["duration"] - 0.1))), 4)
        item["anchor"] = anchor
    if duration is not None:
        item["duration"] = round(max(0.1, float(duration)), 4)
    item["enabled"] = True
    if lock:
        item["locked"] = True
    return f"Title '{item['text']}' at {item['t']:.2f}s"


def delete_item(edl: dict, item_id: str) -> str:
    found = edlmod.find_item(edl, item_id)
    if found is None:
        raise EdlOpError(f"unknown item {item_id}")
    kind, item = found
    if kind == "clips":
        return remove_clip(edl, item_id)
    edl[kind] = [x for x in edl[kind] if x is not item]
    return f"Deleted {kind[:-1]} {item.get('type', item.get('text', ''))}"


def toggle_effect(edl: dict, item_id: str, enabled: bool | None = None, lock: bool = True) -> str:
    found = edlmod.find_item(edl, item_id)
    if found is None or found[0] != "effects":
        raise EdlOpError(f"unknown effect {item_id}")
    e = found[1]
    e["enabled"] = (not e.get("enabled", True)) if enabled is None else bool(enabled)
    if lock:
        e["locked"] = True
    return f"{e['type']} at {e['t']:.2f}s {'on' if e['enabled'] else 'off'}"


def effect_params(etype: str, intensity: str, preset: dict, fps: int) -> tuple[dict, float | None]:
    cfg = (preset.get("effects") or {}).get(etype) or {}
    if etype == "shake":
        return ({"amplitude": pick(cfg.get("amplitude", 0.02), intensity), "rotation_deg": pick(cfg.get("rotation_deg", 0.8), intensity)},
                pick(cfg.get("duration_ms", 180), intensity) / 1000.0)
    if etype == "flash":
        frames = pick(cfg.get("frames", 3), intensity)
        return {"frames": frames}, frames / fps
    if etype == "chromatic":
        return {"px": pick(cfg.get("px", 4), intensity)}, cfg.get("frames", 3) / fps
    if etype == "zoom_punch":
        return {"scale": pick(cfg.get("scale", 1.08), intensity)}, cfg.get("ease_ms", 200) / 1000.0
    if etype == "push_in":
        return {"scale": pick(cfg.get("scale", 1.06), intensity)}, None
    if etype == "glitch":
        frames = pick(cfg.get("frames", 2), intensity) or 2
        return {"frames": frames}, frames / fps
    if etype == "fade_black":
        return {}, cfg.get("duration", 0.5)
    if etype == "motion_blur":
        return {"strength": {"low": 0.6, "med": 1.0, "high": 1.4}[intensity]}, 0.45
    return {}, None


def set_effect_intensity(edl: dict, item_id: str, intensity: str, lock: bool = True) -> str:
    if intensity not in ("low", "med", "high"):
        raise EdlOpError("intensity must be low, med or high")
    found = edlmod.find_item(edl, item_id)
    if found is None or found[0] != "effects":
        raise EdlOpError(f"unknown effect {item_id}")
    e = found[1]
    params, dur = effect_params(e["type"], intensity, load_preset(edl["style"]), edl["fps"])
    e["params"].update(params)
    if dur is not None:
        e["duration"] = round(dur, 4)
    e["intensity"] = intensity
    if lock:
        e["locked"] = True
    return f"{e['type']} at {e['t']:.2f}s set to {intensity}"


def set_all_intensity(edl: dict, intensity: str) -> str:
    edl["intensity"] = intensity
    preset = load_preset(edl["style"])
    for e in edl["effects"]:
        params, dur = effect_params(e["type"], intensity, preset, edl["fps"])
        e["params"].update(params)
        if dur is not None:
            e["duration"] = round(dur, 4)
        e["intensity"] = intensity
    return f"All effects set to {intensity}"


def add_effect(edl: dict, etype: str, t: float, intensity: str | None = None, duration: float | None = None, lock: bool = True) -> dict:
    intensity = intensity or edl.get("intensity", "med")
    params, dur = effect_params(etype, intensity, load_preset(edl["style"]), edl["fps"])
    e = {"id": edlmod.new_item_id("e"), "type": etype, "t": round(max(0.0, float(t)), 4),
         "duration": round(duration if duration is not None else (dur or 0.2), 4), "intensity": intensity, "enabled": True,
         "locked": lock, "params": params}
    edl["effects"].append(e)
    edl["effects"].sort(key=lambda x: x["t"])
    return e


def set_speed_ramp(edl: dict, item_id: str, slow_rate: float | None = 0.4, peak: float | None = None, lock: bool = True) -> str:
    c = _clip(edl, item_id)
    if slow_rate is None or slow_rate >= 0.99:
        c["speed"] = None
        msg = f"Removed the speed ramp on {c['label']}"
    else:
        pk = float(peak) if peak is not None else (c.get("peak") if c.get("peak") is not None else (c["in"] + c["out"]) / 2)
        pk = min(max(pk, c["in"]), c["out"])
        c["peak"] = round(pk, 4)
        c["speed"] = hero_ramp(pk, slow_rate=float(slow_rate))
        msg = f"Speed ramp on {c['label']}: {slow_rate:.2f}x around {pk:.2f}s"
    if lock:
        c["locked_range"] = True
    edlmod.relayout(edl)
    return msg


def set_lock(edl: dict, item_id: str, locked: bool) -> str:
    found = edlmod.find_item(edl, item_id)
    if found is None:
        raise EdlOpError(f"unknown item {item_id}")
    kind, item = found
    if kind == "clips":
        item["locked_order"] = locked
        item["locked_range"] = locked
    else:
        item["locked"] = locked
    return f"{'Pinned' if locked else 'Unpinned'} {item.get('label') or item.get('type') or item.get('text')}"


def set_song_window(edl: dict, start: float, end: float) -> str:
    edl["audio"]["song_window"] = {"start": round(float(start), 3), "end": round(float(end), 3)}
    edl["audio"]["window_locked"] = True
    edl["audio"]["sound_offset"] = round(float(start), 3)
    return f"Song window {start:.1f}-{end:.1f}s (locked)"


def rebuild_text(edl: dict, title: str | None = None) -> str:
    """Regenerate text only: reset the title to the preset default placement, keep clips."""
    preset = load_preset(edl["style"])
    style = dict(preset.get("text", {}))
    title = title or edl.get("title") or "THE ANIMAL"
    hero = next((c for c in edl["clips"] if c["role"] == "hero"), None)
    anim = style.get("animation", "flash_in")
    if anim == "fade":
        t = (hero["start"] + 0.4) if hero else 0.5
        duration = style.get("hold", 2.0) + 2 * style.get("fade", 1.0)
        if hero:
            duration = min(duration, max(1.5, hero["tl_duration"] - 0.2))
    else:
        t = edl["markers"].get("hero_peak") or (edlmod.peak_timeline(hero) if hero else 0.0) or 0.0
        duration = style.get("hold", 0.9)
    markers = [x for x in edl["text"] if x.get("kind") == "marker"]
    edl["text"] = [{"id": edlmod.new_item_id("t"), "text": title, "t": round(t, 4), "duration": round(duration, 4),
                    "animation": anim, "locked": False, "enabled": True, "style": style}] + markers
    edl["title"] = title
    return f"Title reset: '{title}' at {t:.2f}s"

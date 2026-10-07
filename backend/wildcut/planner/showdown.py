"""Showdown preset: animal stat comparison cards with ticking counters, stamps and a winner reveal.

Stats sheet: Claude drafts each value with a source URL and a one-line sourced fact; the sheet is
stored on the project and reviewed in the UI. Values with no source block export until Tim
confirms or edits them (`blockers`). The EDL carries `showdown` data plus `kind: "card"` clips
that the card renderer draws from a layout JSON (presets/showdown_layouts/<layout>.json).
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from wildcut.claude import BudgetTracker, ClaudeClient, get_client
from wildcut.config import get_settings
from wildcut.music.analysis import BeatGridData, auto_window
from wildcut.planner import edl as edlmod
from wildcut.planner.presets import load_preset, pick

log = logging.getLogger(__name__)


class StatRow(BaseModel):
    animal: str = Field(description="common name, lowercase, e.g. 'peregrine falcon'")
    value: float = Field(description="numeric value in the requested unit")
    source_url: str = Field(description="a real URL that states this value (encyclopedia, museum, journal, zoo)")
    fact: str = Field(description="one line (under 14 words) about this animal relevant to the stat")
    fact_source_url: str = Field(description="URL supporting the fact; may equal source_url")


class StatSheet(BaseModel):
    rows: list[StatRow]


STATS_SYSTEM = (
    "You draft a sourced stat sheet for an animal comparison video. Give the best-documented value for each "
    "animal in the requested unit, and only cite URLs you are confident exist (Wikipedia, National Geographic, "
    "Smithsonian, Britannica, peer-reviewed journals, zoos). If unsure of a URL, use the Wikipedia article URL "
    "for the species. Keep facts short and literal."
)


def stat_config(preset: dict, stat: str) -> dict:
    stats = preset.get("showdown", {}).get("stats", {})
    return stats.get(stat) or stats.get("custom")


def draft_stats(stat: str, animals: list[str] | None, unit: str, custom_label: str = "",
                client: ClaudeClient | None = None, budget: BudgetTracker | None = None) -> list[dict]:
    """Claude drafts values + sources. Without Claude the rows are empty and flagged."""
    client = client or get_client()
    label = custom_label or stat.replace("_", " ")
    rows: list[dict] = []
    if client.enabled:
        if animals:
            prompt = (f"Stat: {label} in {unit or 'natural units'}. Animals: {', '.join(animals)}. "
                      f"Return one row per animal in the same order.")
        else:
            prompt = (f"Stat: {label} in {unit or 'natural units'}. Propose 4 to 8 well-known animals in escalating order "
                      f"ending on the record holder, one row each.")
        try:
            sheet = client.structured(prompt, StatSheet, budget=budget, system=STATS_SYSTEM, note="showdown_stats", max_tokens=3000)
            for r in sheet.rows:
                rows.append({"animal": r.animal.strip().lower(), "value": float(r.value), "unit": unit,
                             "source_url": r.source_url.strip(), "fact": r.fact.strip(), "fact_source_url": r.fact_source_url.strip(),
                             "confirmed": False, "clip_id": None})
        except Exception as e:
            log.warning("stats drafting failed: %s", e)
    if not rows:
        for a in (animals or []):
            rows.append({"animal": a.strip().lower(), "value": None, "unit": unit, "source_url": "", "fact": "",
                         "fact_source_url": "", "confirmed": False, "clip_id": None})
    return rows


def blockers(rows: list[dict]) -> list[str]:
    """Animals whose value is missing or unsourced and not confirmed by Tim. Non-empty blocks export."""
    out = []
    for r in rows:
        if r.get("confirmed"):
            if r.get("value") is None:
                out.append(r.get("animal", "?"))
            continue
        if r.get("value") is None or not (r.get("source_url") or "").strip():
            out.append(r.get("animal", "?"))
    return out


def write_stats_csv(rows: list[dict], stat_label: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["animal", stat_label, "unit", "source_url", "fact", "fact_source_url", "confirmed"])
        for r in rows:
            w.writerow([r.get("animal"), r.get("value"), r.get("unit"), r.get("source_url"), r.get("fact"),
                        r.get("fact_source_url"), "yes" if r.get("confirmed") else "no"])
    return path


def load_layout(layout_id: str) -> dict:
    path = get_settings().presets_dir / "showdown_layouts" / f"{layout_id}.json"
    if not path.exists():
        path = get_settings().presets_dir / "showdown_layouts" / "default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def list_layouts() -> list[dict]:
    out = []
    for p in sorted((get_settings().presets_dir / "showdown_layouts").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d.get("id", p.stem), "name": d.get("name", p.stem), "description": d.get("description", "")})
        except Exception:
            continue
    return out


def _fmt_value(v: float | None) -> str:
    if v is None:
        return "?"
    if abs(v) >= 100 or float(v).is_integer():
        return f"{int(round(v)):,}"
    return f"{v:.1f}"


def plan_showdown(project_id: str, aspect: str, mode: str, seed: int, showdown: dict, grid: BeatGridData | None,
                  song_path: str | None, song_window: dict | None, audio_export: str, media: dict[str, dict],
                  winner_clip: dict | None = None, intensity: str | None = None, fps: int = 30,
                  existing: dict | None = None, target_length: float | None = None) -> dict:
    """media: animal -> {"src": path, "proxy": path}. winner_clip: {"src","proxy","in","out","clip_id"}."""
    preset = load_preset("showdown")
    cfg = preset["showdown"]
    intensity = intensity or preset.get("default_intensity", "med")
    stat = showdown.get("stat", "top_speed")
    scfg = stat_config(preset, stat)
    rows = [r for r in showdown.get("stats", []) if r.get("value") is not None]
    higher = scfg.get("higher_wins", True)
    order = sorted(rows, key=lambda r: (r["value"] if higher else -r["value"]))  # escalating to the record holder
    edl = edlmod.empty_edl(project_id, "showdown", aspect, mode, seed, fps)
    edl["intensity"] = intensity
    edl["grade"] = dict(preset.get("grade", {}))
    edl["overlays"] = dict(preset.get("overlays", {}))
    edl["transitions"] = dict(preset.get("transitions", {}))
    edl["audio"].update({"export": audio_export, "song_path": song_path})
    label = showdown.get("custom_label") or scfg.get("label", "STAT")
    unit = showdown.get("unit") or scfg.get("default_unit", "")
    animals = []
    for r in order:
        m = media.get(r["animal"], {})
        animals.append({"name": r["animal"], "value": r["value"], "value_text": _fmt_value(r["value"]), "unit": unit,
                        "fact": r.get("fact", ""), "source_url": r.get("source_url", ""), "src": m.get("src"), "proxy": m.get("proxy")})
    edl["showdown"] = {"stat": stat, "label": label, "unit": unit, "stamp": scfg.get("stamp", "LOSER"),
                       "winner_header": scfg.get("winner_header", "WINNER"), "layout": showdown.get("layout") or cfg.get("layout", "default"),
                       "animals": animals, "higher_wins": higher}
    edl["title"] = scfg.get("winner_header", "WINNER")
    if len(animals) < 2:
        edl["notes"].append("Showdown needs at least two animals with values.")
        return edlmod.relayout(edl)

    n_ch = len(animals) - 1   # challengers
    # ---- timing
    if mode == "music" and grid and grid.beats:
        window = song_window or (existing or {}).get("audio", {}).get("song_window") or auto_window(grid, target_length or (n_ch * 1.8 + 8))
        ws, we = float(window["start"]), float(window["end"])
        drop = grid.chosen_drop if grid.chosen_drop is not None and ws < grid.chosen_drop < we else None
        downs = [round(d - ws, 4) for d in grid.downbeats_in(ws, we)]
        beats = [round(b - ws, 4) for b in grid.beats_in(ws, we)]
        hits = [round(b - ws, 4) for b in grid.bass_hits_in(ws, we)]
        if drop is None and downs:
            drop = ws + downs[min(len(downs) - 1, max(1, int(len(downs) * 0.6)))]
        D = round(drop - ws, 4) if drop is not None else n_ch * cfg["challenger_seconds"] + 1.6
        before = [d for d in downs if d < D - 1e-6]
        bars_per = 1
        if len(before) >= 2 * n_ch + 1 and (grid.beat_period() * 4) < 1.5:
            bars_per = 2
        need = n_ch * bars_per
        if len(before) < need:
            bars_per = 1
            need = n_ch
        if len(before) >= need:
            bounds = before[-need::bars_per] + [D]
            intro_end = bounds[0]
        else:
            # not enough downbeats before the drop: fixed rhythm ending on the drop
            seg = cfg["challenger_seconds"]
            bounds = [max(0.0, D - seg * (n_ch - i)) for i in range(n_ch)] + [D]
            intro_end = bounds[0]
            edl["notes"].append("Song window too short for one challenger per bar; used a fixed rhythm into the drop.")
        segments = list(zip(bounds[:-1], bounds[1:]))
        after = [d for d in downs if d > D + cfg["winner_seconds"] - 0.3]
        winner_end = after[0] if after else min(we - ws, D + cfg["winner_seconds"])
        total_end = we - ws
        edl["audio"]["song_window"] = {"start": round(ws, 3), "end": round(we, 3)}
        edl["audio"]["sound_offset"] = round(ws, 3)
        edl["markers"] = {"drop": D, "hero_peak": D, "beats": beats, "downbeats": downs, "bass_hits": hits}
    else:
        seg = cfg["challenger_seconds"]
        intro_end = 1.4
        segments = [(intro_end + i * seg, intro_end + (i + 1) * seg) for i in range(n_ch)]
        D = segments[-1][1]
        winner_end = D + cfg["winner_seconds"]
        total_end = winner_end + (cfg["winner_clip_seconds"] if winner_clip else 0.0)
        hits, downs, beats = [], [], []
        edl["markers"] = {"drop": None, "hero_peak": D, "beats": [], "downbeats": [], "bass_hits": []}

    # ---- card clips
    def card(ctype: str, start: float, end: float, **params) -> dict:
        d = round(max(0.1, end - start), 4)
        return {"id": edlmod.new_item_id("k"), "kind": "card", "clip_id": None, "moment_id": None, "label": ctype,
                "src": "", "proxy": "", "in": 0.0, "out": d, "start": round(start, 4), "tl_duration": d, "speed": None,
                "crop_path": None, "role": ctype, "locked_order": False, "locked_range": False, "anchor": None,
                "peak": None, "enabled": True, "card": {"type": ctype, **params}}

    clips = [card("intro", 0.0, intro_end, left=0, right=None)]
    champion = 0
    for i, (s, e) in enumerate(segments):
        challenger = i + 1
        cv, chv = animals[champion]["value"], animals[challenger]["value"]
        challenger_wins = (chv > cv) if higher else (chv < cv)
        loser = "left" if challenger_wins else "right"
        clips.append(card("versus", s, e, left=champion, right=challenger, loser=loser, stamp=edl["showdown"]["stamp"]))
        if challenger_wins:
            champion = challenger
    clips.append(card("winner", D, winner_end, left=champion, right=None))
    if winner_clip and winner_clip.get("src"):
        wc_len = max(1.0, min(cfg["winner_clip_seconds"], float(winner_clip.get("out", 0)) - float(winner_clip.get("in", 0)) or cfg["winner_clip_seconds"]))
        end = min(total_end, winner_end + wc_len) if total_end > winner_end + 0.5 else winner_end + wc_len
        clips.append({"id": edlmod.new_item_id("c"), "clip_id": winner_clip.get("clip_id"), "moment_id": winner_clip.get("moment_id"),
                      "label": "winner clip", "src": winner_clip["src"], "proxy": winner_clip.get("proxy") or winner_clip["src"],
                      "in": float(winner_clip.get("in", 0.0)), "out": float(winner_clip.get("in", 0.0)) + (end - winner_end),
                      "start": round(winner_end, 4), "tl_duration": round(end - winner_end, 4), "speed": None,
                      "crop_path": winner_clip.get("crop_path"), "role": "post", "locked_order": False, "locked_range": False,
                      "anchor": None, "peak": None, "enabled": True})
    edl["clips"] = clips
    edl["showdown"]["winner"] = champion
    edlmod.relayout(edl)
    # ---- effects: subtle shake on bass hits, flash on the drop
    fx_cfg = preset.get("effects", {})
    effects = []
    shake = fx_cfg.get("shake")
    if shake and hits:
        ms = pick(shake.get("duration_ms", 140), intensity) / 1000.0
        for h in hits:
            if h <= edl["duration"]:
                effects.append({"id": edlmod.new_item_id("e"), "type": "shake", "t": h, "duration": ms, "intensity": intensity,
                                "enabled": True, "locked": False, "params": {"amplitude": pick(shake["amplitude"], intensity),
                                                                               "rotation_deg": pick(shake["rotation_deg"], intensity)}})
    flash = fx_cfg.get("flash")
    if flash and edl["markers"]["drop"] is not None:
        frames = pick(flash.get("frames", 3), intensity)
        effects.append({"id": edlmod.new_item_id("e"), "type": "flash", "t": edl["markers"]["drop"], "duration": frames / fps,
                        "intensity": intensity, "enabled": True, "locked": False, "params": {"frames": frames}})
    edl["effects"] = effects
    edl["sections"] = [{"name": "intro", "start": 0.0, "end": round(intro_end, 3)},
                       {"name": "challengers", "start": round(intro_end, 3), "end": round(D, 3)},
                       {"name": "winner", "start": round(D, 3), "end": round(edl["duration"], 3)}]
    return edl

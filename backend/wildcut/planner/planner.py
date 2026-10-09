"""Edit planner: scored moments + beat grid + preset -> EDL.

Music-synced: the song window is split into build / drop / post. The build is short beat-locked
cuts tightening toward the drop; the single best moment lands its peak exactly on the drop with
a speed ramp; post-drop uses the next-best moments cut on downbeats. Shakes and flashes go on
bass hits, zoom punches on downbeats. Visual-peaks: a setup / escalation / payoff arc with
effects on each clip's motion peak.

Locks: clips with locked_order keep their relative order (and anchors), clips with
locked_range keep in/out, locked text and effects are carried over untouched, and a locked
song window is never moved. Everything else is re-planned from the seed.
"""
from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field

from wildcut.analysis.moments import Moment
from wildcut.analysis.tracking import crop_size
from wildcut.music.analysis import BeatGridData, auto_window
from wildcut.planner import edl as edlmod
from wildcut.planner.presets import load_preset, pick
from wildcut.planner.speed import (
    hero_ramp,
    source_at,
    timeline_between,
    timeline_duration,
)

MIN_SLOT = 0.25
UNKNOWN_SPECIES = {"", "animal", "none", "unknown", "auto", "any"}
PACE_MULT = {"hard": 1.0, "medium": 2.0, "slow": 4.0}
STOP_WORDS = {"the", "a", "an", "of", "and", "its", "it", "is", "to", "from", "by", "with", "on", "in"}


def _words(text: str) -> set[str]:
    """Lowercase content words (no stemming; see _overlap)."""
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if w not in STOP_WORDS and len(w) >= 3}


def _overlap(a: set[str], b: set[str]) -> bool:
    """Two word sets share a word when one word is a prefix of the other with at least 4 letters in common:
    iguana/iguanas, escape/escapes/escaping, swing/swings, fight/fighting."""
    for x in a:
        for y in b:
            short, long_ = (x, y) if len(x) <= len(y) else (y, x)
            if len(short) >= 4 and long_.startswith(short):
                return True
            if short == long_:
                return True
    return False


@dataclass
class ClipInfo:
    id: str
    label: str
    path: str
    proxy: str
    duration: float
    width: int
    height: int
    species: str = ""
    src_crop: list | None = None

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 16 / 9


@dataclass
class PlanRequest:
    project_id: str
    style: str
    aspect: str
    mode: str                       # music | visual
    target_length: float | None     # None = match song window
    seed: int
    clips: list[ClipInfo]
    moments: list[Moment]
    grid: BeatGridData | None = None
    song_path: str | None = None
    song_window: dict | None = None
    audio_export: str = "silent"
    intensity: str | None = None
    existing: dict | None = None    # previous EDL (locks are honored)
    options: dict = field(default_factory=dict)   # title, banned_moments, starred_moments, ...
    fps: int = 30


class Planner:
    def __init__(self, req: PlanRequest):
        self.req = req
        self.preset = load_preset(req.style)
        self.rng = random.Random(req.seed)
        self.intensity = req.intensity or self.preset.get("default_intensity", "med")
        self.clips_by_id = {c.id: c for c in req.clips}
        self.notes: list[str] = []
        self.used_moments: set[str] = set()
        self.existing = req.existing or {}
        self.banned = set(req.options.get("banned_moments", []))
        self.starred = set(req.options.get("starred_moments", []))
        # what the edit is about: {"subject": "iguana", "action": "escape"}; moments showing the subject rank
        # above everything else, the subject's key action is the hero, other animals only build tension
        focus = req.options.get("focus") or {}
        self.focus_subject = _words(focus.get("subject", ""))
        # the action's words minus the subject and minus other animals in the pool: "escapes the snakes"
        # must not make every snake shot look like the key action
        others: set[str] = set()
        for m in req.moments:
            if m.species and m.species not in UNKNOWN_SPECIES:
                others |= _words(m.species)
        self.focus_action = {w for w in _words(focus.get("action", "")) if not _overlap({w}, self.focus_subject) and not _overlap({w}, others - self.focus_subject)}
        self.pool = [m for m in req.moments if m.id not in self.banned and m.clip_id in self.clips_by_id
                     and m.out_t - m.in_t >= 0.3]
        # jitter in a stable order (clip label, source times): moment ids are random uuids, and sorting by
        # them made two plans of the same footage and seed differ from run to run
        stable = sorted(self.pool, key=lambda m: (self.clips_by_id[m.clip_id].label, self.clips_by_id[m.clip_id].path, m.in_t, m.peak_t, m.id))
        self.jitter = {m.id: self.rng.uniform(0.0, 0.08) for m in stable}
        self.edl = edlmod.empty_edl(req.project_id, req.style, req.aspect, req.mode, req.seed, req.fps)
        self.edl["intensity"] = self.intensity
        self.edl["grade"] = dict(self.preset.get("grade", {}))
        self.edl["overlays"] = dict(self.preset.get("overlays", {}))
        look = str(req.options.get("look") or "normal")
        if look != "normal":
            from wildcut.services.edl_ops import apply_look

            self.edl["grade"], self.edl["overlays"] = apply_look(self.edl["grade"], self.edl["overlays"], look)
        self.edl["transitions"] = dict(self.preset.get("transitions", {}))
        self.edl["audio"]["export"] = req.audio_export
        self.edl["audio"]["song_path"] = req.song_path
        self.edl["audio"]["window_locked"] = bool(self.existing.get("audio", {}).get("window_locked"))
        self.edl["title"] = self.existing.get("title")
        self._shifted = False

    # ------------------------------------------------------------------ scoring helpers
    def adjusted(self, m: Moment) -> float:
        s = m.score + self.jitter.get(m.id, 0.0)
        if m.id in self.starred:
            s += 0.5
        if m.subject_visible is False:
            s -= 0.2
        return s + self.focus_bonus(m)

    def focus_bonus(self, m: Moment) -> float:
        """+0.6 when the moment shows the subject, +0.4 more for its key action (+0.2 extra when the subject
        is the moment's own species, not just mentioned), -0.5 when the subject is absent."""
        if not self.focus_subject:
            return 0.0
        tier = self.focus_tier(m)
        if tier == 0:
            return -0.5
        bonus = 0.6 + (0.2 if _overlap(self.focus_subject, _words(m.species)) else 0.0)
        if tier >= 2:
            bonus += 0.4
        return bonus

    def focus_tier(self, m: Moment) -> int:
        """0 = subject absent, 1 = subject shown, 2 = subject + key action in the caption, 3 = subject + key action
        is the moment's own action tag (the subject doing the thing, e.g. species iguana / action escape)."""
        if not self.focus_subject:
            return 0
        species, action, caption = _words(m.species), _words(m.action), _words(m.caption_hint)
        if not _overlap(self.focus_subject, species | action | caption):
            return 0
        if self.focus_action and _overlap(self.focus_action, action) and _overlap(self.focus_subject, species):
            return 3
        if self.focus_action and _overlap(self.focus_action, action | caption):
            return 2
        return 1

    def matches_focus(self, m: Moment, need_action: bool = False) -> bool:
        tier = self.focus_tier(m)
        return tier >= (2 if (need_action and self.focus_action) else 1)

    def is_fully_locked(self, locked: list[dict]) -> bool:
        """Tim pinned the whole order (intro/outro sections added by a preset do not count)."""
        core = [c for c in locked if c.get("role") not in ("intro", "outro")]
        if not core or not all(c.get("locked_order") for c in core):
            return False
        existing = [c for c in self.existing.get("clips", []) if c.get("enabled", True) and c.get("role") not in ("intro", "outro")]
        return len(core) == len(existing)

    def choose_hero(self) -> Moment | None:
        """The moment that lands on the drop (or the visual payoff). Presets may override.
        With a focus, the hero is the subject's key action when any moment shows it, else any moment of the subject."""
        if self.focus_subject:
            saved = self.pool
            for min_tier in (3, 2, 1):
                cands = [m for m in saved if self.focus_tier(m) >= min_tier]
                if cands:
                    self.pool = cands
                    try:
                        m = self.best_unused(min_len=0.8) or self.best_unused()
                    finally:
                        self.pool = saved
                    if m is not None:
                        return m
        return self.best_unused(min_len=0.8)

    def raw_rank(self, m: Moment) -> float:
        """Deterministic rank (no seed jitter) used when choosing the hero among locked clips."""
        return m.score + (0.5 if m.id in self.starred else 0.0) - (0.2 if m.subject_visible is False else 0.0) + self.focus_bonus(m)

    def best_unused(self, prev_clip: str | None = None, prev_species: str | None = None,
                    min_len: float = 0.0, exclude_clip_ids: set[str] | None = None,
                    prefer_category: str | None = None) -> Moment | None:
        best, best_s = None, -1e9
        species_count = len({m.species for m in self.pool if m.species not in UNKNOWN_SPECIES})
        for m in self.pool:
            if m.id in self.used_moments:
                continue
            if exclude_clip_ids and m.clip_id in exclude_clip_ids:
                continue
            shot_len = (m.shot_end - m.shot_start) if m.shot_end > m.shot_start else self.clips_by_id[m.clip_id].duration
            if shot_len + 1e-6 < min_len:
                continue
            s = self.adjusted(m)
            if prev_clip and m.clip_id == prev_clip:
                s -= 0.15
            if prev_species and species_count > 1 and m.species == prev_species:
                s -= 0.05
            if prefer_category and m.category and m.category != prefer_category:
                s -= 0.3
            if s > best_s:
                best, best_s = m, s
        return best

    def allow_reuse_if_needed(self) -> None:
        if all(m.id in self.used_moments for m in self.pool):
            self.notes.append("Footage pool too small: moments were reused.")
            self.used_moments.clear()

    # ------------------------------------------------------------------ clip entries
    def crop_for(self, m: Moment, in_t: float, out_t: float) -> dict:
        cp = m.crop_paths.get(self.req.aspect)
        if cp:
            return cp
        clip = self.clips_by_id[m.clip_id]
        cw, ch = crop_size(clip.aspect, edlmod.ASPECT_RATIOS[self.req.aspect])
        return {"aspect": edlmod.ASPECT_RATIOS[self.req.aspect], "cw": cw, "ch": ch,
                "keys": [{"t": in_t, "cx": 0.5, "cy": 0.5}, {"t": out_t, "cx": 0.5, "cy": 0.5}]}

    def make_clip(self, m: Moment, in_t: float, out_t: float, role: str, speed: list | None = None,
                  item_id: str | None = None, **extra) -> dict:
        clip = self.clips_by_id[m.clip_id]
        in_t = round(max(0.0, in_t), 4)
        out_t = round(min(clip.duration, out_t), 4)
        if out_t - in_t < 0.2:
            out_t = min(clip.duration, in_t + 0.2)
        entry = {
            "id": item_id or edlmod.new_item_id("c"), "clip_id": m.clip_id, "moment_id": m.id, "label": clip.label,
            "src": clip.path, "proxy": clip.proxy, "in": in_t, "out": out_t, "start": 0.0, "tl_duration": 0.0,
            "speed": speed, "crop_path": self.crop_for(m, in_t, out_t), "role": role,
            "locked_order": False, "locked_range": False, "anchor": None,
            "peak": round(min(max(m.peak_t, in_t), out_t), 4), "enabled": True,
            "species": m.species, "action": m.action, "caption_hint": m.caption_hint,
        }
        if clip.src_crop:
            entry["src_crop"] = list(clip.src_crop)
        if self.req.options.get("frame"):
            entry["frame"] = self.req.options["frame"]       # project default framing (Inspector "apply to all")
        entry.update(extra)
        self.used_moments.add(m.id)
        return entry

    def anchored_hero(self, locked: list[dict]) -> dict | None:
        """A locked title anchored inside a locked clip makes that clip the hero (peak = the anchor)."""
        for t in self.existing.get("text", []):
            a = t.get("anchor")
            if t.get("locked") and a:
                for c in locked:
                    if c["id"] == a.get("item_id") or c.get("clip_id") == a.get("clip_id"):
                        c = dict(c)
                        c["peak_override"] = float(a.get("source_time", c.get("peak") or c["in"]))
                        return c
        return None

    def moment_for_existing(self, c: dict) -> Moment:
        m = next((m for m in self.req.moments if m.id == c.get("moment_id")), None)
        if m is not None and c.get("peak_override") is not None:
            m = Moment.from_dict(m.to_dict())
            m.peak_t = float(c["peak_override"])
        if m is None:
            clip = self.clips_by_id.get(c["clip_id"])
            m = Moment(id=c.get("moment_id") or f"m_{c['id']}", clip_id=c["clip_id"], in_t=c["in"], out_t=c["out"],
                       peak_t=c.get("peak") if c.get("peak") is not None else (c["in"] + c["out"]) / 2,
                       motion_score=0.5, shot_start=0.0, shot_end=clip.duration if clip else c["out"],
                       species=c.get("species", ""), action=c.get("action", ""))
            if c.get("peak_override") is not None:
                m.peak_t = float(c["peak_override"])
        return m

    # ------------------------------------------------------------------ locks
    def locked_clips(self) -> list[dict]:
        out = []
        for c in self.existing.get("clips", []):
            if (c.get("locked_order") or c.get("locked_range")) and c["clip_id"] in self.clips_by_id:
                out.append(c)
        return out

    def carry_locked_text(self) -> list[dict]:
        return [dict(t) for t in self.existing.get("text", []) if t.get("locked")]

    def carry_locked_effects(self) -> list[dict]:
        return [dict(e) for e in self.existing.get("effects", []) if e.get("locked")]

    # ------------------------------------------------------------------ title
    def title_text(self, hero: Moment | None) -> str:
        opt = self.req.options.get("title")
        if opt:
            return opt
        subject = str((self.req.options.get("focus") or {}).get("subject") or "").strip()
        if subject:
            return f"THE {subject.upper()}"      # the edit is about the subject, whatever the hero's species tag says
        if self.existing.get("title"):
            return self.existing["title"]
        species = ""
        if hero and hero.species not in UNKNOWN_SPECIES:
            species = hero.species
        else:
            counts = Counter(m.species for m in self.pool if m.species not in UNKNOWN_SPECIES)
            if counts:
                species = counts.most_common(1)[0][0]
        if not species:
            for c in self.req.clips:
                if c.species and c.species not in UNKNOWN_SPECIES:
                    species = c.species
                    break
        if not species:
            self.notes.append("No species detected; the title is a placeholder. Edit it in the timeline or chat.")
            species = "animal"
        return f"THE {species.upper()}"

    # ------------------------------------------------------------------ entry point
    def plan(self) -> dict:
        if not self.pool:
            self.notes.append("No usable moments: analyze footage first.")
            self.edl["notes"] = self.notes
            return self.edl
        if self.req.mode == "music" and self.req.grid and self.req.grid.beats:
            self.plan_music()
        else:
            if self.req.mode == "music":
                self.notes.append("No beat grid available; planned on visual peaks instead.")
            self.plan_visual()
        self.edl["notes"] = list(dict.fromkeys(self.notes))
        edlmod.relayout(self.edl)
        return self.edl

    # ------------------------------------------------------------------ music mode
    def paced(self, pacing: dict) -> dict:
        """Tim's pace: 'hard' = the preset as written (a cut per beat or two), 'medium' = twice as long,
        'slow' = four times (clips play out; the hero still lands on the drop)."""
        mult = PACE_MULT.get(str(self.req.options.get("pace") or "hard"), 1.0)
        if mult == 1.0:
            return pacing
        out = dict(pacing)
        for k in ("build_cut_beats_start", "build_cut_beats_end", "final_bar_cut_beats", "post_cut_beats", "post_cut_beats_min", "shot_bars"):
            if k in out:
                out[k] = out[k] * mult
        for k in ("visual_lead", "visual_tail", "visual_hero_lead", "visual_hero_tail"):
            if k in out:
                out[k] = out[k] * min(mult, 2.0)
        return out

    def plan_music(self) -> None:
        req, grid, preset = self.req, self.req.grid, self.preset
        pacing = self.paced(preset["pacing"])
        ramp = preset.get("speed_ramp", {})
        window = req.song_window
        if self.existing.get("audio", {}).get("window_locked") and self.existing["audio"].get("song_window"):
            window = self.existing["audio"]["song_window"]
        if not window:
            window = auto_window(grid, req.target_length)
        ws, we = float(window["start"]), float(window["end"])
        L = we - ws
        drop = grid.chosen_drop
        if drop is None or not (ws < drop < we):
            cand = [d for d in grid.downbeats_in(ws, we)]
            drop = min(cand, key=lambda d: abs((d - ws) / L - 0.6)) if cand else ws + 0.6 * L
            self.notes.append("No drop inside the song window; using a downbeat at ~60% as the drop.")
        beats = [round(b - ws, 4) for b in grid.beats_in(ws, we)]
        if not beats or beats[0] > 0.05:
            beats.insert(0, 0.0)
        downbeats = [round(b - ws, 4) for b in grid.downbeats_in(ws, we)]
        hits = [round(b - ws, 4) for b in grid.bass_hits_in(ws, we)]
        period = grid.beat_period()
        D = round(drop - ws, 4)

        # ---- hero
        locked = self.locked_clips()
        hero_locked = self.anchored_hero(locked) or next((c for c in locked if c.get("role") == "hero"), None)
        if hero_locked is not None:
            locked = [hero_locked if c["id"] == hero_locked["id"] else c for c in locked]
            hero_m = self.moment_for_existing(hero_locked)
        elif self.is_fully_locked(locked):
            # fully locked order: the hero is the best moment among the locked clips
            hero_locked = max([c for c in locked if c.get("role") not in ("intro", "outro")], key=lambda c: self.raw_rank(self.moment_for_existing(c)))
            hero_m = self.moment_for_existing(hero_locked)
        else:
            hero_m = self.choose_hero()
        if hero_m is None:
            self.plan_visual()
            return
        fully_locked = self.is_fully_locked(locked)

        # ---- sequence: locked clips keep order; the hero sits at its locked index or after the pre-hero locks
        before, after = [], []
        if hero_locked is not None:
            idx = [c["id"] for c in locked].index(hero_locked["id"])
            before = [c for c in locked[:idx]]
            after = [c for c in locked[idx + 1:]]
        else:
            before = [c for c in locked if c.get("anchor") != "end"]
            after = [c for c in locked if c.get("anchor") == "end"]
        pre_entries = [self.rebuild_locked(c, beats, period, pacing) for c in before]
        post_locked = [self.rebuild_locked(c, beats, period, pacing) for c in after]
        pre_len = sum(c["tl_duration"] for c in pre_entries)

        hero = self.build_hero_clip(hero_m, hero_locked, D, beats, downbeats, period, ramp,
                                    fixed_start=pre_len if fully_locked else None)
        t0 = hero["start"]
        if (fully_locked and abs(pre_len - t0) > 0.5 / req.fps) or pre_len > t0 + 1e-3:
            # the locked clips decide where the hero starts; move the song so the drop meets the peak
            if not self.edl["audio"]["window_locked"] and not self._shifted:
                lead = timeline_between(hero.get("speed"), hero["in"], hero["peak"])
                ws_new = drop - (pre_len + lead)
                ws_new = max(0.0, min(ws_new, grid.duration - L))
                if abs(ws_new - ws) > 1e-3:
                    self._shifted = True
                    self.used_moments.clear()
                    self.notes.append("Moved the song window so the drop lands on the locked hero moment.")
                    req.song_window = {"start": round(ws_new, 3), "end": round(min(grid.duration, ws_new + L), 3)}
                    return self.plan_music()
            if pre_len > t0 + 1e-3:
                self.notes.append("Locked clips before the drop are longer than the build; the drop will not land on the hero peak.")
                hero["start"] = round(pre_len, 4)
                t0 = pre_len
        if fully_locked:
            build_entries = []
            hero["start"] = round(pre_len, 4)
            t0 = pre_len
            hero_end = t0 + hero["tl_duration"]
            post_entries = post_locked
        else:
            build_entries = self.fill_build(pre_len, t0, beats, period, pacing, prev=pre_entries[-1] if pre_entries else None)
            hero_end = t0 + hero["tl_duration"]
            post_entries = self.fill_post(hero_end, L, beats, downbeats, period, pacing, post_locked, prev=hero)

        clips = pre_entries + build_entries + [hero] + post_entries
        self.edl["clips"] = clips
        edlmod.relayout(self.edl)
        # make sure the hero still sits on the drop after relayout (build fill is exact; guard anyway)
        hero_peak_tl = edlmod.peak_timeline(hero)
        if hero_peak_tl is not None and abs(hero_peak_tl - D) > 0.5 / req.fps and build_entries:
            delta = D - hero_peak_tl
            last = build_entries[-1]
            if not last.get("locked_range"):
                last["out"] = round(min(self.clips_by_id[last["clip_id"]].duration, max(last["in"] + 0.2, last["out"] + delta)), 4)
                edlmod.relayout(self.edl)
        hero_peak_tl = edlmod.peak_timeline(hero)

        self.edl["audio"]["song_window"] = {"start": round(ws, 3), "end": round(we, 3)}
        self.edl["audio"]["sound_offset"] = round(ws, 3)
        self.edl["markers"] = {"drop": D, "hero_peak": hero_peak_tl, "beats": beats, "downbeats": downbeats, "bass_hits": hits}
        self.edl["sections"] = [
            {"name": "build", "start": 0.0, "end": round(t0, 3)},
            {"name": "drop", "start": round(t0, 3), "end": round(hero_end, 3)},
            {"name": "post", "start": round(hero_end, 3), "end": round(self.edl["duration"], 3)},
        ]
        self.add_music_effects(beats, downbeats, hits, D, hero)
        self.add_title(hero, hero_peak_tl if hero_peak_tl is not None else D)

    def build_hero_clip(self, m: Moment, locked: dict | None, D: float, beats: list[float], downbeats: list[float],
                        period: float, ramp: dict, fixed_start: float | None = None) -> dict:
        clip = self.clips_by_id[m.clip_id]
        shot_start = m.shot_start if m.shot_end > m.shot_start else 0.0
        shot_end = m.shot_end if m.shot_end > m.shot_start else clip.duration
        peak = m.peak_t
        speed = None
        if ramp.get("hero", True):
            speed = hero_ramp(peak, ramp.get("slow_rate", 0.4), ramp.get("pre_ramp", 0.35), ramp.get("ease", 0.18),
                              ramp.get("hold_after", 0.25))
        if locked is not None and locked.get("locked_range"):
            in_t, out_t = locked["in"], locked["out"]
            speed = locked.get("speed") or speed
        else:
            in_t = max(shot_start, peak - ramp.get("hero_pre", 0.8))
            out_t = min(shot_end, peak + ramp.get("hero_post", 1.2))
            lead = timeline_between(speed, in_t, peak)
            # start the hero on the beat nearest (drop - lead), then solve `in` so the peak hits the drop exactly
            t0_raw = D - lead
            cands = [b for b in beats if b <= D - 0.05]
            t0 = min(cands, key=lambda b: abs(b - t0_raw)) if cands else max(0.0, t0_raw)
            if fixed_start is not None:
                t0 = fixed_start
            target_lead = max(0.05, D - t0)
            lo, hi = shot_start, peak - 0.05
            best_in = in_t
            if timeline_between(speed, lo, peak) < target_lead:
                best_in = lo  # not enough source lead-in: hero starts early and the previous clip absorbs the gap
            else:
                for _ in range(50):
                    mid = (lo + hi) / 2
                    if timeline_between(speed, mid, peak) > target_lead:
                        lo = mid
                    else:
                        hi = mid
                best_in = (lo + hi) / 2
            in_t = best_in
            # end on the first downbeat at least 0.4 s after the peak if the shot allows it
            peak_tl = timeline_between(speed, in_t, peak)
            end_cands = [d for d in downbeats if d >= t0 + peak_tl + 0.4]
            if end_cands:
                target_end = end_cands[0] - t0
                out_cand = source_at(speed, in_t, shot_end, target_end)
                if out_cand <= shot_end and out_cand > peak + 0.3:
                    out_t = out_cand
        entry = self.make_clip(m, in_t, out_t, "hero", speed=speed, item_id=locked["id"] if locked else None)
        if locked:
            entry["locked_order"] = bool(locked.get("locked_order"))
            entry["locked_range"] = bool(locked.get("locked_range"))
            entry["anchor"] = locked.get("anchor")
            carry_frame(entry, locked)
        entry["tl_duration"] = round(timeline_duration(entry["in"], entry["out"], speed), 4)
        lead = timeline_between(speed, entry["in"], entry["peak"])
        entry["start"] = round(max(0.0, D - lead), 4)
        return entry

    def rebuild_locked(self, c: dict, beats: list[float], period: float, pacing: dict) -> dict:
        m = self.moment_for_existing(c)
        role = c.get("role", "build")
        if role == "hero":
            role = "build"   # the hero is chosen fresh each plan; a pinned ex-hero is a normal cut
        c = dict(c, role=role)
        if c.get("locked_range"):
            entry = self.make_clip(m, c["in"], c["out"], c.get("role", "build"), speed=c.get("speed"), item_id=c["id"])
        else:
            # keep the clip but re-trim it to a whole number of beats around its peak
            n_beats = max(1, int(round((c["out"] - c["in"]) / period)))
            d = n_beats * period
            in_t = max(m.shot_start, m.peak_t - pacing.get("peak_position_in_cut", 0.7) * d)
            entry = self.make_clip(m, in_t, in_t + d, c.get("role", "build"), speed=None, item_id=c["id"])
        entry["locked_order"] = bool(c.get("locked_order"))
        entry["locked_range"] = bool(c.get("locked_range"))
        entry["anchor"] = c.get("anchor")
        carry_frame(entry, c)
        entry["tl_duration"] = round(timeline_duration(entry["in"], entry["out"], entry.get("speed")), 4)
        return entry

    def slot_plan(self, start: float, end: float, beats: list[float], period: float, pacing: dict) -> list[tuple[float, float]]:
        """Beat-aligned slots filling [start, end], tightening toward the end."""
        grid = sorted(set([b for b in beats if start - 1e-6 <= b <= end + 1e-6] + [start, end]))
        if len(grid) < 2:
            return [(start, end)] if end - start > MIN_SLOT else []
        if pacing.get("cut_mode") == "bars":
            per = max(1, int(pacing.get("shot_bars", 2))) * 4
            return self._slots_from_grid(grid, lambda p: per, per, period, pacing, start, end)
        s0, s1 = pacing.get("build_cut_beats_start", 2), pacing.get("build_cut_beats_end", 1)
        final = pacing.get("final_bar_cut_beats", 1)
        total = end - start

        def beats_for(p: float) -> float:
            remaining = total * (1 - p)
            if remaining <= 4 * period + 1e-6:
                return final
            return s0 + (s1 - s0) * min(1.0, p / 0.75)

        return self._slots_from_grid(grid, beats_for, 1, period, pacing, start, end)

    def _slots_from_grid(self, grid, beats_for, _unused, period, pacing, start, end):
        slots = []
        cursor = start
        while end - cursor > MIN_SLOT:
            p = (cursor - start) / max(1e-6, end - start)
            nb = beats_for(p)
            target = cursor + nb * period
            if nb >= 1:
                cands = [b for b in grid if b > cursor + 0.5 * period]
                nxt = min(cands, key=lambda b: abs(b - target)) if cands else end
            else:
                nxt = target  # half-beat cuts in the final bar
            nxt = min(nxt, end)
            if end - nxt < MIN_SLOT:
                nxt = end
            slots.append((round(cursor, 4), round(nxt, 4)))
            cursor = nxt
        return slots

    def fill_build(self, start: float, end: float, beats: list[float], period: float, pacing: dict,
                   prev: dict | None) -> list[dict]:
        entries: list[dict] = []
        pos = pacing.get("peak_position_in_cut", 0.7)
        prev_clip = prev["clip_id"] if prev else None
        prev_species = prev.get("species") if prev else None
        cursor = start
        for s, e in self.slot_plan(start, end, beats, period, pacing):
            d = e - s
            if e >= end - 1e-6:
                d = end - cursor  # last slot absorbs any drift so the hero stays on the drop
            if d < MIN_SLOT:
                break
            self.allow_reuse_if_needed()
            m = self.best_unused(prev_clip, prev_species, min_len=d) or self.best_unused(prev_clip, prev_species)
            if m is None:
                break
            clip = self.clips_by_id[m.clip_id]
            shot_start = m.shot_start if m.shot_end > m.shot_start else 0.0
            shot_end = m.shot_end if m.shot_end > m.shot_start else clip.duration
            in_t = max(shot_start, min(m.peak_t - pos * d, shot_end - d))
            out_t = min(shot_end, in_t + d)
            entries.append(self.make_clip(m, in_t, out_t, "build"))
            entries[-1]["tl_duration"] = round(out_t - in_t, 4)
            cursor += out_t - in_t
            prev_clip, prev_species = m.clip_id, m.species
        if entries and end - cursor > 1e-3:
            last = entries[-1]
            clip = self.clips_by_id[last["clip_id"]]
            last["out"] = round(min(clip.duration, last["out"] + (end - cursor)), 4)
            last["tl_duration"] = round(last["out"] - last["in"], 4)
        return entries

    def fill_post(self, start: float, end: float, beats: list[float], downbeats: list[float], period: float,
                  pacing: dict, post_locked: list[dict], prev: dict | None) -> list[dict]:
        entries: list[dict] = []
        cursor = start
        prev_clip = prev["clip_id"] if prev else None
        prev_species = prev.get("species") if prev else None
        pos = pacing.get("peak_position_in_cut", 0.7)
        locked_len = sum(c["tl_duration"] for c in post_locked)
        auto_end = end - locked_len
        nb = pacing.get("post_cut_beats", 4)
        nb_min = pacing.get("post_cut_beats_min", 2)
        if pacing.get("cut_mode") == "bars":
            nb = max(4, int(pacing.get("shot_bars", 2)) * 4)
            nb_min = 4
        while auto_end - cursor > MIN_SLOT:
            cands = [d for d in downbeats if d > cursor + nb_min * period - 1e-6]
            nxt = min(cands, key=lambda d: abs(d - (cursor + nb * period))) if cands else auto_end
            nxt = min(nxt, auto_end)
            if auto_end - nxt < nb_min * period * 0.5:
                nxt = auto_end
            d = nxt - cursor
            self.allow_reuse_if_needed()
            m = self.best_unused(prev_clip, prev_species, min_len=d) or self.best_unused(prev_clip, prev_species)
            if m is None:
                break
            clip = self.clips_by_id[m.clip_id]
            shot_start = m.shot_start if m.shot_end > m.shot_start else 0.0
            shot_end = m.shot_end if m.shot_end > m.shot_start else clip.duration
            in_t = max(shot_start, min(m.peak_t - pos * d, shot_end - d))
            out_t = min(shot_end, in_t + d)
            entries.append(self.make_clip(m, in_t, out_t, "post"))
            entries[-1]["tl_duration"] = round(out_t - in_t, 4)
            cursor += out_t - in_t
            prev_clip, prev_species = m.clip_id, m.species
        if entries and auto_end - cursor > 1e-3:
            last = entries[-1]
            clip = self.clips_by_id[last["clip_id"]]
            last["out"] = round(min(clip.duration, last["out"] + (auto_end - cursor)), 4)
            last["tl_duration"] = round(last["out"] - last["in"], 4)
        # a stubby trailing cut (< 1.5 s) reads as a mistake: fold it into the previous clip when the shot has room
        if len(entries) >= 2 and entries[-1]["tl_duration"] < 1.5 and not entries[-1].get("locked_range"):
            prev, stub = entries[-2], entries[-1]
            m = next((m for m in self.req.moments if m.id == prev["moment_id"]), None)
            room = (m.shot_end if m and m.shot_end > m.shot_start else self.clips_by_id[prev["clip_id"]].duration) - prev["out"]
            if room >= stub["tl_duration"] - 1e-3:
                prev["out"] = round(prev["out"] + stub["tl_duration"], 4)
                prev["tl_duration"] = round(prev["out"] - prev["in"], 4)
                self.used_moments.discard(stub["moment_id"])
                entries.pop()
        return entries + post_locked

    # ------------------------------------------------------------------ effects and text
    def effect(self, etype: str, t: float, duration: float, params: dict | None = None, locked: bool = False) -> dict:
        return {"id": edlmod.new_item_id("e"), "type": etype, "t": round(t, 4), "duration": round(duration, 4),
                "intensity": self.intensity, "enabled": True, "locked": locked, "params": params or {}}

    def add_music_effects(self, beats, downbeats, hits, D, hero) -> None:
        fx_cfg = self.preset.get("effects", {})
        fps = self.req.fps
        effects: list[dict] = self.carry_locked_effects()
        dur = self.edl["duration"]
        shake = fx_cfg.get("shake")
        if shake:
            ms = pick(shake.get("duration_ms", 180), self.intensity) / 1000.0
            for h in hits:
                if h <= dur:
                    effects.append(self.effect("shake", h, ms, {"amplitude": pick(shake.get("amplitude", 0.02), self.intensity),
                                                                 "rotation_deg": pick(shake.get("rotation_deg", 0.8), self.intensity)}))
        flash = fx_cfg.get("flash")
        flash_times: list[float] = []
        if flash:
            frames = pick(flash.get("frames", 3), self.intensity)
            flash_times.append(D)
            on = flash.get("on", "")
            if "bass_hit_every_2" in on:
                flash_times += [h for i, h in enumerate(hits) if i % 2 == 0 and abs(h - D) > 0.1]
            elif "bass_hit" in on:
                flash_times += [h for h in hits if abs(h - D) > 0.1]
            for t in flash_times:
                if t <= dur:
                    effects.append(self.effect("flash", t, frames / fps, {"frames": frames}))
        chroma = fx_cfg.get("chromatic")
        if chroma and flash_times:
            px = pick(chroma.get("px", 4), self.intensity)
            for t in flash_times:
                if t <= dur:
                    effects.append(self.effect("chromatic", t, chroma.get("frames", 3) / fps, {"px": px}))
        zoom = fx_cfg.get("zoom_punch")
        if zoom:
            for d in downbeats:
                if d <= dur:
                    effects.append(self.effect("zoom_punch", d, zoom.get("ease_ms", 200) / 1000.0,
                                               {"scale": pick(zoom.get("scale", 1.08), self.intensity)}))
        glitch = fx_cfg.get("glitch")
        if glitch:
            frames = pick(glitch.get("frames", 2), self.intensity)
            if frames:
                effects.append(self.effect("glitch", D, frames / fps, {"frames": frames}))
        self.add_clip_effects(effects, fx_cfg)
        self.edl["effects"] = effects

    def add_clip_effects(self, effects: list[dict], fx_cfg: dict) -> None:
        push = fx_cfg.get("push_in")
        if push:
            scale = pick(push.get("scale", 1.06), self.intensity)
            for c in self.edl["clips"]:
                effects.append(self.effect("push_in", c["start"], c["tl_duration"], {"scale": scale}))
        fade = fx_cfg.get("fade_black")
        if fade:
            d = fade.get("duration", 0.5)
            heroes = [c for c in self.edl["clips"] if c["role"] == "hero"]
            for h in heroes:
                if h["start"] > d:
                    effects.append(self.effect("fade_black", h["start"] - d / 2, d, {}))
            last = self.edl["clips"][-1] if self.edl["clips"] else None
            if last and last["role"] != "hero" and last["start"] > d:
                effects.append(self.effect("fade_black", last["start"] - d / 2, d, {}))

    def add_title(self, hero: dict, peak_tl: float) -> None:
        text_cfg = self.preset.get("text", {})
        locked = self.carry_locked_text()
        hero_m = next((m for m in self.req.moments if m.id == hero.get("moment_id")), None)
        title = self.title_text(hero_m)
        self.edl["title"] = title
        if locked:
            self.edl["text"] = locked
            return
        anim = text_cfg.get("animation", "flash_in")
        hold = text_cfg.get("hold", 0.9)
        if anim == "fade":
            fade = text_cfg.get("fade", 1.0)
            t = max(0.0, hero["start"] + 0.4)
            duration = hold + 2 * fade
            duration = min(duration, max(1.5, hero["tl_duration"] - 0.2))
        else:
            t = peak_tl
            duration = hold
        self.edl["text"] = [{
            "id": edlmod.new_item_id("t"), "text": title, "t": round(t, 4), "duration": round(duration, 4),
            "animation": anim, "locked": False, "enabled": True, "style": dict(text_cfg),
        }]

    # ------------------------------------------------------------------ visual mode
    def plan_visual(self) -> None:
        req, preset = self.req, self.preset
        pacing = self.paced(preset["pacing"])
        ramp = preset.get("speed_ramp", {})
        L = float(req.target_length or 30.0)
        lead, tail = pacing.get("visual_lead", 1.0), pacing.get("visual_tail", 0.35)
        hlead, htail = pacing.get("visual_hero_lead", 1.2), pacing.get("visual_hero_tail", 1.0)
        locked = self.locked_clips()
        hero_locked = self.anchored_hero(locked) or next((c for c in locked if c.get("role") == "hero"), None)
        if hero_locked is not None:
            locked = [hero_locked if c["id"] == hero_locked["id"] else c for c in locked]
            hero_m = self.moment_for_existing(hero_locked)
        elif self.is_fully_locked(locked):
            hero_locked = max([c for c in locked if c.get("role") not in ("intro", "outro")], key=lambda c: self.raw_rank(self.moment_for_existing(c)))
            hero_m = self.moment_for_existing(hero_locked)
        else:
            hero_m = self.choose_hero()
        if hero_m is None:
            self.notes.append("No usable moments.")
            return
        speed = None
        if ramp.get("hero", True):
            speed = hero_ramp(hero_m.peak_t, ramp.get("slow_rate", 0.4), ramp.get("pre_ramp", 0.35), ramp.get("ease", 0.18),
                              ramp.get("hold_after", 0.25))
        clip = self.clips_by_id[hero_m.clip_id]
        s_start = hero_m.shot_start if hero_m.shot_end > hero_m.shot_start else 0.0
        s_end = hero_m.shot_end if hero_m.shot_end > hero_m.shot_start else clip.duration
        if hero_locked is not None and hero_locked.get("locked_range"):
            hero = self.make_clip(hero_m, hero_locked["in"], hero_locked["out"], "hero", speed=hero_locked.get("speed") or speed, item_id=hero_locked["id"])
        else:
            hero = self.make_clip(hero_m, max(s_start, hero_m.peak_t - hlead), min(s_end, hero_m.peak_t + htail), "hero",
                                  speed=speed, item_id=hero_locked["id"] if hero_locked else None)
        if hero_locked:
            hero["locked_order"] = bool(hero_locked.get("locked_order"))
            hero["locked_range"] = bool(hero_locked.get("locked_range"))
            hero["anchor"] = hero_locked.get("anchor")
        hero["tl_duration"] = round(timeline_duration(hero["in"], hero["out"], hero["speed"]), 4)

        # locked entries keep their ranges (or get the visual trim around their peak)
        def visual_entry(c: dict) -> dict:
            m = self.moment_for_existing(c)
            if c.get("locked_range"):
                e = self.make_clip(m, c["in"], c["out"], c.get("role", "build"), speed=c.get("speed"), item_id=c["id"])
            else:
                cl = self.clips_by_id[m.clip_id]
                ss = m.shot_start if m.shot_end > m.shot_start else 0.0
                se = m.shot_end if m.shot_end > m.shot_start else cl.duration
                e = self.make_clip(m, max(ss, m.peak_t - lead), min(se, m.peak_t + tail), c.get("role", "build"), item_id=c["id"])
            e["locked_order"], e["locked_range"], e["anchor"] = bool(c.get("locked_order")), bool(c.get("locked_range")), c.get("anchor")
            carry_frame(e, c)
            e["tl_duration"] = round(timeline_duration(e["in"], e["out"], e.get("speed")), 4)
            return e

        if hero_locked is not None:
            idx = [c["id"] for c in locked].index(hero_locked["id"])
            before = [visual_entry(c) for c in locked[:idx]]
            after = [visual_entry(c) for c in locked[idx + 1:]]
        else:
            before = [visual_entry(c) for c in locked if c.get("anchor") != "end"]
            after = [visual_entry(c) for c in locked if c.get("anchor") == "end"]
        fully_locked = self.is_fully_locked(locked)

        # auto picks until the target length is reached
        auto: list[dict] = []
        total = sum(c["tl_duration"] for c in before + after) + hero["tl_duration"]
        prev_clip, prev_species = hero["clip_id"], hero.get("species")
        shot_min, shot_max = pacing.get("shot_min", 0.0), pacing.get("shot_max", 0.0)
        while not fully_locked and total < L - 0.5:
            self.allow_reuse_if_needed()
            m = self.best_unused(prev_clip, prev_species, min_len=min(lead + tail, shot_min or 0))
            if m is None:
                break
            cl = self.clips_by_id[m.clip_id]
            ss = m.shot_start if m.shot_end > m.shot_start else 0.0
            se = m.shot_end if m.shot_end > m.shot_start else cl.duration
            in_t, out_t = max(ss, m.peak_t - lead), min(se, m.peak_t + tail)
            if shot_min and out_t - in_t < shot_min:
                out_t = min(se, in_t + shot_min)
                in_t = max(ss, out_t - shot_min)
            if shot_max and out_t - in_t > shot_max:
                in_t = max(ss, out_t - shot_max)
            remaining = L - total
            if out_t - in_t > remaining:
                out_t = in_t + max(0.6, remaining)
            e = self.make_clip(m, in_t, out_t, "build")
            e["tl_duration"] = round(timeline_duration(e["in"], e["out"], None), 4)
            auto.append(e)
            total += e["tl_duration"]
            prev_clip, prev_species = m.clip_id, m.species
        if total < L - 2.0 and not fully_locked:
            self.notes.append(f"Edit is {total:.1f}s, shorter than the {L:.0f}s target: not enough strong footage.")
        # arc: setup/escalation (ascending score) -> payoff (hero) -> remaining best moments descending
        auto.sort(key=lambda e: self.adjusted(next(m for m in self.pool if m.id == e["moment_id"])))
        n_before = int(round(len(auto) * 0.65))
        setup, post = auto[:n_before], auto[n_before:]
        post.sort(key=lambda e: -self.adjusted(next(m for m in self.pool if m.id == e["moment_id"])))
        for e in setup:
            e["role"] = "build"
        for e in post:
            e["role"] = "post"
        self.edl["clips"] = before + setup + [hero] + post + after
        edlmod.relayout(self.edl)
        peak_tl = edlmod.peak_timeline(hero)
        self.edl["markers"] = {"drop": None, "hero_peak": peak_tl, "beats": [], "downbeats": [], "bass_hits": []}
        self.edl["sections"] = [
            {"name": "setup", "start": 0.0, "end": round(hero["start"], 3)},
            {"name": "payoff", "start": round(hero["start"], 3), "end": round(hero["start"] + hero["tl_duration"], 3)},
            {"name": "post", "start": round(hero["start"] + hero["tl_duration"], 3), "end": round(self.edl["duration"], 3)},
        ]
        self.add_visual_effects(hero)
        self.add_title(hero, peak_tl if peak_tl is not None else hero["start"])

    def add_visual_effects(self, hero: dict) -> None:
        fx_cfg = self.preset.get("effects", {})
        fps = self.req.fps
        effects = self.carry_locked_effects()
        kinds = fx_cfg.get("visual_peak", [])
        for c in self.edl["clips"]:
            pt = edlmod.peak_timeline(c)
            if pt is None:
                continue
            for kind in kinds:
                cfg = fx_cfg.get(kind)
                if not cfg:
                    continue
                if kind == "shake":
                    effects.append(self.effect("shake", pt, pick(cfg.get("duration_ms", 180), self.intensity) / 1000.0,
                                               {"amplitude": pick(cfg.get("amplitude", 0.02), self.intensity),
                                                "rotation_deg": pick(cfg.get("rotation_deg", 0.8), self.intensity)}))
                elif kind == "flash":
                    frames = pick(cfg.get("frames", 3), self.intensity)
                    effects.append(self.effect("flash", pt, frames / fps, {"frames": frames}))
                    chroma = fx_cfg.get("chromatic")
                    if chroma:
                        effects.append(self.effect("chromatic", pt, chroma.get("frames", 3) / fps, {"px": pick(chroma.get("px", 4), self.intensity)}))
                elif kind == "zoom_punch":
                    effects.append(self.effect("zoom_punch", pt, cfg.get("ease_ms", 200) / 1000.0, {"scale": pick(cfg.get("scale", 1.08), self.intensity)}))
        self.add_clip_effects(effects, fx_cfg)
        self.edl["effects"] = effects


def carry_frame(entry: dict, old: dict) -> None:
    """A clip whose framing / freeze Tim set by hand keeps it across re-plans (the key is present even when None)."""
    if "frame" in old:
        entry["frame"] = old["frame"]
    if old.get("hold") and entry["in"] - 1e-6 <= float(old["hold"].get("at", -1)) <= entry["out"] + 1e-6:
        entry["hold"] = dict(old["hold"])


def plan(req: PlanRequest) -> dict:
    return Planner(req).plan()

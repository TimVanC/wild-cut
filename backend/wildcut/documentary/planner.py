"""Documentary edit planner: intro (BROLL -> AURA reveal), beat-locked HERO/AURA build, the best HERO
on the drop with the title, HERO post-drop on downbeats, AURA/BROLL outro. BROLL never pads the middle.

The shot set for each edit is chosen by `partition_shots` so no HERO or AURA shot appears in two
edits and the edits open on different shots with different hero moments.
"""
from __future__ import annotations

from wildcut.analysis.moments import Moment
from wildcut.planner.planner import Planner, PlanRequest

INTRO_MIN, INTRO_MAX = 3.0, 8.0
OUTRO_MIN, OUTRO_MAX = 3.0, 6.0


def partition_shots(hero: list[dict], aura: list[dict], broll: list[dict], n: int) -> list[dict]:
    """Round-robin by score so edits are balanced and distinct. Returns one {hero, aura, broll} per edit."""
    hero = sorted(hero, key=lambda s: -s["score"])
    aura = sorted(aura, key=lambda s: -s["score"])
    broll = sorted(broll, key=lambda s: -s["score"])
    sets = [{"hero": [], "aura": [], "broll": []} for _ in range(n)]
    for i, s in enumerate(hero):
        sets[i % n]["hero"].append(s)
    for i, s in enumerate(aura):
        sets[i % n]["aura"].append(s)
    for k in range(n):
        # BROLL may repeat; rotate so each edit opens on a different establishing shot
        rot = broll[k % len(broll):] + broll[:k % len(broll)] if broll else []
        sets[k]["broll"] = rot
    return sets


class DocumentaryPlanner(Planner):
    """Phonk planner with category-aware slot filling and intro / outro sections."""

    def __init__(self, req: PlanRequest):
        super().__init__(req)
        self.category_pref: list[str] = ["hero", "aura"]
        self.category_exclude: set[str] = {"broll"}
        self.breather_used = False

    def best_unused(self, prev_clip=None, prev_species=None, min_len=0.0, exclude_clip_ids=None, prefer_category=None):
        saved = self.pool
        allowed = [m for m in saved if (m.category or "hero") not in self.category_exclude]
        if allowed:
            self.pool = allowed
        try:
            for pref in self.category_pref:
                cand = [m for m in self.pool if (m.category or "hero") == pref and m.id not in self.used_moments]
                if cand:
                    self.pool = cand
                    m = super().best_unused(prev_clip, prev_species, min_len, exclude_clip_ids, None)
                    self.pool = allowed if allowed else saved
                    if m is not None:
                        return m
            return super().best_unused(prev_clip, prev_species, min_len, exclude_clip_ids, None)
        finally:
            self.pool = saved

    def allow_reuse_if_needed(self) -> None:
        """Within one edit a HERO/AURA shot may supply several cuts; exclusivity holds across edits."""
        core = [m for m in self.pool if (m.category or "hero") in ("hero", "aura")]
        if core and all(m.id in self.used_moments for m in core):
            for m in core:
                self.used_moments.discard(m.id)
            if "HERO/AURA moments were reused" not in " ".join(self.notes):
                self.notes.append("HERO/AURA moments were reused within this edit to reach the target length.")

    def choose_hero(self) -> Moment | None:
        heroes = [m for m in self.pool if (m.category or "hero") == "hero" and m.id not in self.used_moments]
        if heroes:
            return max(heroes, key=self.adjusted)
        return super().choose_hero()

    def _entries_for(self, cats: list[str], total: float, unit: float, role: str, max_items: int, prev=None) -> list[dict]:
        """Clips of ~unit seconds each from the given categories until `total` is covered."""
        out: list[dict] = []
        covered = 0.0
        prev_clip = prev["clip_id"] if prev else None
        for cat in cats:
            while covered < total - 0.3 and len(out) < max_items:
                cands = [m for m in self.pool if (m.category or "") == cat and m.id not in self.used_moments]
                if not cands:
                    break
                m = max(cands, key=lambda x: self.adjusted(x) - (0.15 if x.clip_id == prev_clip else 0.0))
                clip = self.clips_by_id[m.clip_id]
                shot_start = m.shot_start if m.shot_end > m.shot_start else 0.0
                shot_end = m.shot_end if m.shot_end > m.shot_start else clip.duration
                d = min(unit, total - covered, shot_end - shot_start)
                if d < 0.6:
                    self.used_moments.add(m.id)
                    continue
                in_t = max(shot_start, min(m.peak_t - 0.5 * d, shot_end - d))
                e = self.make_clip(m, in_t, in_t + d, role)
                e["tl_duration"] = round(e["out"] - e["in"], 4)
                out.append(e)
                covered += e["tl_duration"]
                prev_clip = m.clip_id
                if cat == "broll" and len([x for x in out if (x.get("category") or "") == "broll"]) >= 2:
                    break
            if covered >= total - 0.3:
                break
        return out

    def make_clip(self, m, in_t, out_t, role, speed=None, item_id=None, **extra):
        e = super().make_clip(m, in_t, out_t, role, speed=speed, item_id=item_id, **extra)
        e["category"] = m.category
        return e

    def plan_music(self) -> None:
        req, grid = self.req, self.req.grid
        if not grid or not grid.beats:
            return super().plan_music()
        period = grid.beat_period()
        bar = 4 * period
        # intro: 1-2 bars of BROLL then an AURA reveal (1 bar), total 3-8 s
        self.category_pref, self.category_exclude = ["broll"], set()
        intro = self._entries_for(["broll"], min(INTRO_MAX - bar, 2 * bar), bar, "intro", 2)
        self.category_pref = ["aura", "hero"]
        intro += self._entries_for(["aura", "hero"], bar, bar, "intro", 1, prev=intro[-1] if intro else None)
        intro_len = sum(e["tl_duration"] for e in intro)
        if intro_len < INTRO_MIN:
            intro += self._entries_for(["aura", "broll", "hero"], INTRO_MIN - intro_len, bar, "intro", 2, prev=intro[-1] if intro else None)
        # outro: an AURA portrait or a BROLL wide, 3-6 s (2 bars)
        self.category_pref = ["aura", "broll"]
        outro = self._entries_for(["aura", "broll"], min(OUTRO_MAX, 2 * bar), 2 * bar, "outro", 1)
        if outro and outro[0]["tl_duration"] < OUTRO_MIN:
            outro[0]["out"] = round(min(self.clips_by_id[outro[0]["clip_id"]].duration, outro[0]["in"] + OUTRO_MIN), 4)
            outro[0]["tl_duration"] = round(outro[0]["out"] - outro[0]["in"], 4)
        for e in outro:
            e["anchor"] = "end"
            e["locked_order"] = True
        for e in intro:
            e["locked_order"] = True
        # hand intro/outro to the base planner as locked clips; the build/post fill uses HERO (and AURA) only
        self.category_pref, self.category_exclude = ["hero", "aura"], {"broll"}
        existing = dict(self.existing)
        existing["clips"] = intro + outro + [c for c in existing.get("clips", []) if c.get("locked_order") or c.get("locked_range")]
        self.existing = existing
        for e in intro + outro:
            self.used_moments.add(e["moment_id"])
        # shorten the target when the hero pool cannot cover a full-length edit instead of padding with weak shots
        hero_sec = sum(m.out_t - m.in_t for m in self.pool if (m.category or "hero") == "hero" and m.id not in self.used_moments)
        aura_sec = sum(m.out_t - m.in_t for m in self.pool if (m.category or "") == "aura" and m.id not in self.used_moments)
        window = req.song_window or {}
        L = (window.get("end", 0) - window.get("start", 0)) if window else (req.target_length or 65.0)
        middle = L - intro_len - sum(e["tl_duration"] for e in outro)
        if hero_sec + 0.5 * aura_sec < 0.8 * middle:
            new_middle = max(12.0, hero_sec + 0.5 * aura_sec)
            new_L = round(intro_len + new_middle + sum(e["tl_duration"] for e in outro), 1)
            self.notes.append(f"Shorter edit ({new_L:.0f}s): only {hero_sec:.0f}s of strong HERO footage was available for this edit.")
            req.target_length = new_L
            req.song_window = None
        super().plan_music()
        for c in self.edl["clips"]:
            if c.get("role") in ("intro", "outro"):
                c["locked_order"] = False
        self.edl["sections"] = [{"name": "intro", "start": 0.0, "end": round(intro_len, 3)}] + [
            s for s in self.edl["sections"]]

    def plan_visual(self) -> None:
        self.category_pref, self.category_exclude = ["broll"], set()
        intro = self._entries_for(["broll"], 4.0, 2.5, "intro", 2)
        self.category_pref = ["aura", "hero"]
        intro += self._entries_for(["aura", "hero"], 2.5, 2.5, "intro", 1, prev=intro[-1] if intro else None)
        self.category_pref = ["aura", "broll"]
        outro = self._entries_for(["aura", "broll"], 4.0, 4.0, "outro", 1)
        for e in outro:
            e["anchor"] = "end"
            e["locked_order"] = True
        for e in intro:
            e["locked_order"] = True
        self.category_pref, self.category_exclude = ["hero", "aura"], {"broll"}
        existing = dict(self.existing)
        existing["clips"] = intro + outro + [c for c in existing.get("clips", []) if c.get("locked_order") or c.get("locked_range")]
        self.existing = existing
        for e in intro + outro:
            self.used_moments.add(e["moment_id"])
        super().plan_visual()
        for c in self.edl["clips"]:
            if c.get("role") in ("intro", "outro"):
                c["locked_order"] = False


def plan_documentary(req: PlanRequest) -> dict:
    return DocumentaryPlanner(req).plan()

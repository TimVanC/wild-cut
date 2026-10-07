"""Chase preset: predator, prey, outcome in three beats with the full Phonk treatment.

The planner pairs a predator moment and a prey moment that read as the same scene (habitat,
lighting, dominant color from Claude's tags) and alternates predator / prey cuts through the
build. The outcome lands on the drop:
  caught  -> the predator's strike with the speed ramp, a motion-blur pass, cut to black with an
             optional emoji marker; the predator's name is the only title.
  escaped -> the prey's evasion with the speed ramp; the prey's name is the only title.
The variant is picked from Claude's `outcome` / `escape` tags unless Tim forces one.
"""
from __future__ import annotations

from wildcut.analysis.moments import Moment
from wildcut.planner import edl as edlmod
from wildcut.planner.planner import Planner, PlanRequest

PREDATOR_ACTIONS = {"sprint", "pounce", "strike", "catch", "dive", "fight"}
PREY_ACTIONS = {"sprint", "leap", "escape", "swing", "dive"}


def pairs_table(preset: dict) -> list[tuple[str, str]]:
    return [tuple(p) for p in preset.get("chase", {}).get("pairs", [])]


def classify(species: str, preset: dict) -> str:
    """predator | prey | unknown by the built-in pairs table."""
    s = (species or "").lower()
    for pred, prey in pairs_table(preset):
        if s == pred:
            return "predator"
        if s == prey:
            return "prey"
    return "unknown"


class ChasePlanner(Planner):
    def __init__(self, req: PlanRequest):
        super().__init__(req)
        self.variant = "auto"
        self.pred_moments: list[Moment] = []
        self.prey_moments: list[Moment] = []
        self.pick_toggle = 0
        self.pair_bonus: dict[str, float] = {}
        self.hero_choice: Moment | None = None

    # ---- pairing -----------------------------------------------------------------
    def classify_pool(self) -> None:
        for m in self.pool:
            role = classify(m.species, self.preset)
            if role == "unknown":
                # motion-only fallback: fast "sprint" moments with no species are treated as predators
                role = "predator" if m.action in ("pounce", "strike", "catch") else ("prey" if m.action in ("escape", "leap") else "unknown")
            if role == "predator":
                self.pred_moments.append(m)
            elif role == "prey":
                self.prey_moments.append(m)
        if self.pred_moments and self.prey_moments:
            # only paired species are eligible; other animals never leak into a chase
            keep = {m.id for m in self.pred_moments} | {m.id for m in self.prey_moments}
            self.pool = [m for m in self.pool if m.id in keep]
        if not self.pred_moments or not self.prey_moments:
            # no clear pairing: split the pool by clip so the structure still alternates two subjects
            clips = sorted({m.clip_id for m in self.pool})
            if len(clips) >= 2:
                a, b = clips[0], clips[1]
                self.pred_moments = [m for m in self.pool if m.clip_id == a] or self.pool
                self.prey_moments = [m for m in self.pool if m.clip_id == b] or self.pool
                self.notes.append("No predator/prey species tags; alternating the first two clips as predator and prey.")
            else:
                self.pred_moments = list(self.pool)
                self.prey_moments = list(self.pool)
                self.notes.append("Only one clip: Chase structure needs a predator clip and a prey clip.")

    def scene_match(self, a: Moment, b: Moment) -> float:
        s = 0.0
        if a.habitat and a.habitat == b.habitat:
            s += 0.3
        if a.dominant_color and a.dominant_color == b.dominant_color:
            s += 0.15
        if a.lighting and a.lighting == b.lighting:
            s += 0.1
        return s

    def choose_pair(self) -> tuple[Moment, Moment]:
        best, best_s = None, -1e9
        for p in self.pred_moments:
            for q in self.prey_moments:
                if p.id == q.id:
                    continue
                s = self.adjusted(p) + self.adjusted(q) + self.scene_match(p, q)
                if s > best_s:
                    best, best_s = (p, q), s
        assert best is not None
        p, q = best
        # moments that match the chosen scene get a bonus when filling the build
        for m in self.pool:
            self.pair_bonus[m.id] = self.scene_match(m, p) + self.scene_match(m, q)
        return p, q

    def decide_variant(self, p: Moment, q: Moment) -> str:
        forced = self.req.options.get("chase_variant") or self.preset.get("chase", {}).get("outcome", "auto")
        if forced in ("caught", "escaped"):
            return forced
        outcomes = [m.outcome for m in self.pool if m.outcome in ("caught", "escaped")]
        if any(m.action == "escape" for m in self.prey_moments) or outcomes.count("escaped") > outcomes.count("caught"):
            return "escaped"
        return "caught"

    # ---- planner hooks -----------------------------------------------------------
    def adjusted(self, m: Moment) -> float:
        return super().adjusted(m) + self.pair_bonus.get(m.id, 0.0)

    def best_unused(self, prev_clip=None, prev_species=None, min_len=0.0, exclude_clip_ids=None, prefer_category=None):
        # alternate predator / prey picks through the build
        group = self.pred_moments if self.pick_toggle % 2 == 0 else self.prey_moments
        self.pick_toggle += 1
        saved_pool = self.pool
        self.pool = [m for m in group if m.id not in self.used_moments] or saved_pool
        try:
            m = super().best_unused(prev_clip, prev_species, min_len, exclude_clip_ids, prefer_category)
        finally:
            self.pool = saved_pool
        if m is None:
            m = super().best_unused(prev_clip, prev_species, min_len, exclude_clip_ids, prefer_category)
        return m

    def choose_hero(self) -> Moment | None:
        if self.hero_choice is not None and self.hero_choice.id not in self.used_moments:
            return self.hero_choice
        return super().choose_hero()

    def plan(self) -> dict:
        if not self.pool:
            return super().plan()
        self.classify_pool()
        p, q = self.choose_pair()
        self.variant = self.decide_variant(p, q)
        if self.variant == "escaped":
            cands = [m for m in self.prey_moments if m.action in ("escape", "leap", "sprint")] or self.prey_moments
            self.hero_choice = max(cands, key=self.adjusted)
            self.title_species = q.species
        else:
            cands = [m for m in self.pred_moments if m.action in ("pounce", "strike", "catch", "sprint")] or self.pred_moments
            self.hero_choice = max(cands, key=self.adjusted)
            self.title_species = p.species
        # force the hero by starring it heavily
        self.starred = set(self.starred) | {self.hero_choice.id}
        length = self.req.target_length or 15.0
        length = min(max(length, self.preset["chase"]["length_min"]), self.preset["chase"]["length_max"])
        self.req.target_length = length
        edl = super().plan()
        edl["chase"] = {"variant": self.variant, "predator": p.species, "prey": q.species,
                        "predator_moment": p.id, "prey_moment": q.id}
        self.add_outcome(edl)
        return edl

    def title_text(self, hero: Moment | None) -> str:
        if self.req.options.get("title") or self.existing.get("title"):
            return super().title_text(hero)
        species = getattr(self, "title_species", "") or (hero.species if hero else "")
        if species and species not in ("animal", "none", "unknown"):
            return f"THE {species.upper()}"
        return super().title_text(hero)

    def add_outcome(self, edl: dict) -> None:
        hero = next((c for c in edl["clips"] if c["role"] == "hero"), None)
        if hero is None:
            return
        cfg = self.preset.get("chase", {})
        fps = self.req.fps
        hero_end = hero["start"] + hero["tl_duration"]
        if self.variant == "caught":
            style = cfg.get("outcome_style", "blur_pass")
            if style == "blur_pass":
                edl["effects"].append(self.effect("motion_blur", max(hero["start"], hero_end - 0.45), 0.45, {"strength": 1.0}))
            # cut to black for a beat after the strike, with an optional emoji marker
            dip = 0.6
            edl["effects"].append(self.effect("fade_black", hero_end - 0.08, dip, {"hard": True}))
            marker = cfg.get("outcome_marker", "")
            if marker and self.req.options.get("outcome_marker", True):
                edl["text"].append({
                    "id": edlmod.new_item_id("t"), "text": marker, "t": round(hero_end, 4), "duration": round(dip * 0.8, 4),
                    "animation": "fade", "locked": False, "enabled": True, "kind": "marker",
                    "style": {"font": "emoji", "size_frac": 0.14, "letter_spacing": 0, "color": "#FFFFFF", "shadow": 0,
                              "fade": 0.08, "position": [0.5, 0.5]},
                })
        edl["sections"] = [s for s in edl.get("sections", [])]
        for s in edl["sections"]:
            if s["name"] == "drop":
                s["name"] = "outcome" if self.variant == "caught" else "escape"


def plan_chase(req: PlanRequest) -> dict:
    return ChasePlanner(req).plan()

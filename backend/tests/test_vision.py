"""Vision tagging with a fake Claude client (no network) and the no-key heuristic fallback."""
from __future__ import annotations

from pathlib import Path

from wildcut.analysis.moments import Moment, build_moments, score_moment
from wildcut.analysis.motion import compute_motion
from wildcut.analysis.shots import detect_shots
from wildcut.analysis.vision import MomentTag, MomentTagBatch, tag_moments
from wildcut.claude import BudgetExceeded, BudgetTracker, ClaudeClient, estimate_cost
from wildcut.media import probe


class FakeClaude(ClaudeClient):
    """Returns canned tags; records how many calls and images it saw."""

    def __init__(self, species="cheetah", fail_after: int | None = None):
        super().__init__(api_key="fake", model="claude-sonnet-5-5")
        self.species = species
        self.calls = 0
        self.images = 0
        self.fail_after = fail_after

    def structured(self, content, schema, budget=None, system=None, max_tokens=4000, note=""):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise BudgetExceeded("Claude budget exhausted")
        if budget:
            budget.add(0.01, note)
        n = sum(1 for b in content if b.get("type") == "text" and b["text"].startswith("Moment "))
        self.images += sum(1 for b in content if b.get("type") == "image")
        return MomentTagBatch(tags=[
            MomentTag(index=i, species=self.species, action="sprint" if i % 2 == 0 else "idle", intensity=9 - i,
                      framing=7, subject_visible=True, caption_hint=f"moment {i}", habitat="savanna",
                      lighting="golden", dominant_color="yellow", outcome="none", category="hero")
            for i in range(n)
        ])


def _moments(assets, name="static_peaks"):
    path = Path(assets["dir"]) / assets["clips"][name]["path"]
    info = probe(path)
    curve = compute_motion(path)
    return path, build_moments(name, detect_shots(path, info.duration), curve, info.width / info.height)


def test_tagging_batches_and_scores(assets):
    path, moments = _moments(assets)
    fake = FakeClaude()
    budget = BudgetTracker(limit_usd=1.5)
    tag_moments(path, moments, budget=budget, client=fake)
    assert fake.calls >= 1
    assert fake.images >= 3 * len(moments[:4])
    assert all(m.species == "cheetah" for m in moments)
    assert budget.spent_usd > 0 and budget.calls == fake.calls
    sprint = [m for m in moments if m.action == "sprint"]
    idle = [m for m in moments if m.action == "idle"]
    if sprint and idle:
        assert max(m.score for m in sprint) > max(m.score for m in idle)


def test_idle_and_hidden_subject_penalized():
    base = Moment(id="a", clip_id="c", in_t=0, out_t=2, peak_t=1, motion_score=0.8, intensity=8, framing=8, subject_visible=True, action="leap")
    idle = Moment(id="b", clip_id="c", in_t=0, out_t=2, peak_t=1, motion_score=0.8, intensity=8, framing=8, subject_visible=True, action="idle")
    hidden = Moment(id="d", clip_id="c", in_t=0, out_t=2, peak_t=1, motion_score=0.8, intensity=8, framing=8, subject_visible=False, action="leap")
    assert score_moment(base) > score_moment(idle)
    assert score_moment(base) > score_moment(hidden)


def test_budget_exhaustion_falls_back_to_heuristics(assets):
    path, moments = _moments(assets)
    fake = FakeClaude(fail_after=0)
    tag_moments(path, moments, budget=BudgetTracker(limit_usd=1.5), client=fake)
    assert all(m.species == "animal" for m in moments)
    assert all(m.action for m in moments)


def test_no_key_uses_heuristics(assets):
    path, moments = _moments(assets)
    tag_moments(path, moments, client=ClaudeClient(api_key="", model="x"))
    assert all(m.species == "animal" and m.intensity >= 1 for m in moments)


def test_budget_tracker_blocks_when_exhausted():
    b = BudgetTracker(limit_usd=0.05)
    b.add(0.04, "x")
    b.check()
    b.add(0.02, "y")
    try:
        b.check()
        raise AssertionError("should have raised")
    except BudgetExceeded:
        pass
    assert estimate_cost("claude-sonnet-5-5", 1_000_000, 0) == 2.0

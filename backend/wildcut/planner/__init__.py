"""Planner dispatch: picks the planner for a preset's `structure` (single | chase | showdown)."""
from __future__ import annotations

from wildcut.planner.presets import load_preset


def plan_for_preset(req) -> dict:
    """PlanRequest -> EDL for single-animal and Chase presets (Showdown has its own entry point)."""
    preset = load_preset(req.style)
    structure = preset.get("structure", "single")
    if structure == "chase":
        from wildcut.planner.chase import plan_chase

        return plan_chase(req)
    from wildcut.planner.planner import plan

    return plan(req)

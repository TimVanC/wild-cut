"""Speed curves: piecewise-linear playback rate over source time, with exact timeline mapping.

A curve is a list of keyframes [{"t": source_seconds, "rate": r}], sorted by t. Between two
keyframes the rate interpolates linearly; outside the first/last keyframe it is constant.
Timeline time elapsed between source times a and b is the integral of 1/rate.
"""
from __future__ import annotations

import math
from typing import Sequence

Key = dict


def _rate_at(keys: Sequence[Key], t: float) -> float:
    if not keys:
        return 1.0
    if t <= keys[0]["t"]:
        return float(keys[0]["rate"])
    if t >= keys[-1]["t"]:
        return float(keys[-1]["rate"])
    for a, b in zip(keys, keys[1:]):
        if a["t"] <= t <= b["t"]:
            if b["t"] == a["t"]:
                return float(b["rate"])
            f = (t - a["t"]) / (b["t"] - a["t"])
            return float(a["rate"] + (b["rate"] - a["rate"]) * f)
    return 1.0


def _segment_time(r0: float, r1: float, length: float) -> float:
    """Timeline time to traverse `length` source seconds while rate goes linearly r0 -> r1."""
    if length <= 0:
        return 0.0
    if abs(r1 - r0) < 1e-9:
        return length / r0
    return length / (r1 - r0) * math.log(r1 / r0)


def timeline_between(keys: Sequence[Key] | None, a: float, b: float) -> float:
    """Timeline seconds elapsed from source time a to source time b (a <= b)."""
    if b <= a:
        return 0.0
    if not keys:
        return b - a
    total = 0.0
    bounds = [a] + [k["t"] for k in keys if a < k["t"] < b] + [b]
    for s0, s1 in zip(bounds, bounds[1:]):
        total += _segment_time(_rate_at(keys, s0), _rate_at(keys, s1), s1 - s0)
    return total


def timeline_duration(in_t: float, out_t: float, keys: Sequence[Key] | None) -> float:
    return timeline_between(keys, in_t, out_t)


def source_at(keys: Sequence[Key] | None, in_t: float, out_t: float, elapsed: float) -> float:
    """Source time reached after `elapsed` timeline seconds from in_t (bisection, monotonic)."""
    if elapsed <= 0:
        return in_t
    if not keys:
        return min(out_t, in_t + elapsed)
    lo, hi = in_t, out_t
    if timeline_between(keys, in_t, out_t) <= elapsed:
        return out_t
    for _ in range(60):
        mid = (lo + hi) / 2
        if timeline_between(keys, in_t, mid) < elapsed:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2


def hero_ramp(peak: float, slow_rate: float = 0.4, pre_ramp: float = 0.35, ease: float = 0.18,
              hold_after: float = 0.25) -> list[Key]:
    """1x -> slow into the peak, hold through it, ease back to 1x. All times are source seconds."""
    return [
        {"t": round(peak - pre_ramp - ease, 4), "rate": 1.0},
        {"t": round(peak - pre_ramp, 4), "rate": slow_rate},
        {"t": round(peak + hold_after, 4), "rate": slow_rate},
        {"t": round(peak + hold_after + ease, 4), "rate": 1.0},
    ]


def constant(rate: float) -> list[Key]:
    return [{"t": 0.0, "rate": rate}]

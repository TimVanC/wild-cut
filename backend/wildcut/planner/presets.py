"""Style presets are JSON files in presets/. A preset may `extends` another; dicts merge deeply."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from wildcut.config import get_settings

INTENSITIES = ("low", "med", "high")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def presets_dir() -> Path:
    return get_settings().presets_dir


def list_presets() -> list[dict]:
    out = []
    for p in sorted(presets_dir().glob("*.json")):
        try:
            d = load_preset(p.stem)
            out.append({"id": d["id"], "name": d.get("name", d["id"]), "description": d.get("description", ""),
                        "default_aspect": d.get("default_aspect", "9:16"), "structure": d.get("structure", "single")})
        except Exception:
            continue
    return out


@lru_cache(maxsize=32)
def _load_raw(preset_id: str) -> dict:
    path = presets_dir() / f"{preset_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"unknown preset '{preset_id}' (expected {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def load_preset(preset_id: str, _depth: int = 0) -> dict:
    raw = _load_raw(preset_id)
    parent = raw.get("extends")
    if parent and _depth < 4:
        base = load_preset(parent, _depth + 1)
        merged = _deep_merge(base, {k: v for k, v in raw.items() if k != "extends"})
        merged["id"] = raw.get("id", preset_id)
        return merged
    return copy.deepcopy(raw)


def pick(values, intensity: str):
    """Resolve a {"low":..,"med":..,"high":..} mapping (or a scalar) for an intensity level."""
    if isinstance(values, dict) and any(k in values for k in INTENSITIES):
        if intensity in values:
            return values[intensity]
        return values.get("med", next(iter(values.values())))
    return values


def clear_cache() -> None:
    _load_raw.cache_clear()

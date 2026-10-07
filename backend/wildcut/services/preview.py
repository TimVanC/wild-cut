"""Chunked preview renders: the timeline is split into fixed 2 s chunks, each rendered to its own
MP4 keyed by a hash of everything that affects it. After an EDL change only the chunks whose key
changed are re-rendered; the chunks are then stitched (stream copy) into one preview MP4."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable

from wildcut.media import MediaError
from wildcut.planner import edl as edlmod
from wildcut.render.composer import RenderSettings, render_video
from wildcut.services.projects import project_dir

CHUNK_SECONDS = 2.0
PREVIEW_WIDTH = 540


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - 1e-9 and b0 < a1 - 1e-9


def chunk_key(edl: dict, t0: float, t1: float, width: int) -> str:
    parts = {
        "aspect": edl["aspect"], "fps": edl["fps"], "style": edl["style"], "intensity": edl.get("intensity"), "width": width,
        "grade": edl.get("grade"), "overlays": edl.get("overlays"), "transitions": edl.get("transitions"), "duration": edl["duration"],
        "showdown": edl.get("showdown"),
        "clips": [c for c in edl["clips"] if c.get("enabled", True) and _overlaps(c["start"], c["start"] + c["tl_duration"], t0, t1)],
        "effects": [e for e in edl.get("effects", []) if e.get("enabled", True) and _overlaps(e["t"], e["t"] + max(e["duration"], 1 / edl["fps"]), t0 - 0.1, t1 + 0.1)],
        "text": [x for x in edl.get("text", []) if x.get("enabled", True) and _overlaps(x["t"], x["t"] + x["duration"], t0, t1)],
        "range": [round(t0, 4), round(t1, 4)],
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render_preview(project_id: str, edl: dict, version: int, progress: Callable[[float, str], None] | None = None,
                   width: int = PREVIEW_WIDTH) -> Path:
    pdir = project_dir(project_id)
    chunks_dir = pdir / "chunks"
    duration = float(edl["duration"])
    if duration <= 0:
        raise MediaError("EDL is empty")
    n = int(-(-duration // CHUNK_SECONDS))
    bounds = [(i * CHUNK_SECONDS, min(duration, (i + 1) * CHUNK_SECONDS)) for i in range(n)]
    files = []
    todo = []
    for i, (t0, t1) in enumerate(bounds):
        key = chunk_key(edl, t0, t1, width)
        f = chunks_dir / f"{key}.mp4"
        files.append(f)
        if not f.exists():
            todo.append((i, t0, t1, f))
    settings = RenderSettings(quality="preview", width=width, smooth_slowmo=False)
    for j, (i, t0, t1, f) in enumerate(todo):
        if progress:
            progress(j / max(1, len(todo)), f"preview chunk {i + 1}/{n}")
        tmp = f.with_suffix(".tmp.mp4")
        render_video(edl, tmp, settings, segment=(t0, t1))
        tmp.replace(f)
    out = pdir / "previews" / f"v{version}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    concat_chunks(files, out)
    if progress:
        progress(1.0, "preview ready")
    # keep the chunk cache bounded
    prune_chunks(chunks_dir, keep=set(f.name for f in files), max_files=400)
    return out


def concat_chunks(files: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c", "copy", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode != 0:
        raise MediaError(f"concat failed: {r.stderr.strip()[-600:]}")
    return out


def prune_chunks(chunks_dir: Path, keep: set[str], max_files: int) -> None:
    files = sorted(chunks_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    excess = len(files) - max_files
    for f in files:
        if excess <= 0:
            break
        if f.name not in keep:
            try:
                f.unlink()
                excess -= 1
            except OSError:
                pass


def changed_ranges(old: dict | None, new: dict, width: int = PREVIEW_WIDTH) -> list[tuple[float, float]]:
    """Timeline ranges whose chunks would re-render (for the UI's "re-rendering 4.0-6.0s" hint)."""
    duration = float(new["duration"])
    n = int(-(-duration // CHUNK_SECONDS))
    out = []
    for i in range(n):
        t0, t1 = i * CHUNK_SECONDS, min(duration, (i + 1) * CHUNK_SECONDS)
        if old is None or chunk_key(old, t0, t1, width) != chunk_key(new, t0, t1, width):
            out.append((t0, t1))
    return out

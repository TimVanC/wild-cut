"""Shot bank pipeline: letterbox -> cached proxy -> shots -> dedupe -> cheap filters -> motion ->
Claude classification (keyframes only, batched, within budget) -> scored HERO/AURA/BROLL/OTHER.

Everything expensive is cached under data/documentaries/<file key>/ so generating more edits
later never reprocesses the film.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from pydantic import BaseModel, Field

from wildcut.analysis.motion import MotionCurve, compute_motion, finalize_curve
from wildcut.analysis.shots import Shot, detect_shots_adaptive
from wildcut.claude import BudgetTracker, ClaudeClient, get_client, image_block
from wildcut.config import get_settings
from wildcut.documentary.filters import dhash, hamming, has_text, is_black
from wildcut.documentary.letterbox import crop_rect, detect_letterbox
from wildcut.media import MediaError, extract_frames, frame_to_jpeg_b64, probe, thumbnail

log = logging.getLogger(__name__)
Progress = Callable[[float, str], None]
MIN_SHOT = 0.5
SHOTS_PER_CALL = 12
CATEGORIES = ("hero", "aura", "broll", "other")


@dataclass
class BankShot:
    index: int
    start: float
    end: float
    duration: float
    category: str = ""           # hero | aura | broll | other
    species: str = ""
    caption: str = ""
    intensity: int = 0
    framing: int = 0
    max_motion: float = 0.0
    peaks: list[float] = field(default_factory=list)       # absolute film times
    score: float = 0.0
    rejected: str = ""           # black | short | text | people | duplicate | logo
    duplicate_of: int | None = None
    hash_first: int = 0
    hash_mid: int = 0
    thumb: str = ""
    classified_by: str = ""      # claude | heuristic

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BankShot":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class DocShotTag(BaseModel):
    index: int
    category: Literal["hero", "aura", "broll", "other"] = Field(description="hero = the target animal doing something (hunting, leaping, fighting, fleeing, swinging, diving); aura = the target animal close-up or posing (stare, slow turn, silhouette, portrait); broll = landscape/habitat with no clear animal; other = a different animal, or people/presenters/crew")
    species: str = Field(description="main animal common name, lowercase; 'none' if no animal")
    has_people: bool = Field(description="people, presenters, crew, hands, vehicles visible")
    has_text: bool = Field(description="burned-in text, subtitles, lower thirds, title cards, maps, logos")
    caption: str = Field(description="under 10 words describing the shot")
    intensity: int = Field(ge=1, le=10, description="how dramatic the action is (1 for still shots)")
    framing: int = Field(ge=1, le=10, description="composition quality for a vertical social edit")


class DocShotBatch(BaseModel):
    tags: list[DocShotTag]


DOC_SYSTEM = ("You classify shots from a nature documentary for a short-form edit about one target animal. "
              "Answer only with the requested JSON. Be literal about what is visible in the frame.")


def file_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def cache_dir_for(path: Path) -> Path:
    d = get_settings().data_dir / "documentaries" / file_key(path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_proxy(src: Path, dst: Path, crop: tuple[int, int, int, int] | None, height: int = 540) -> Path:
    if dst.exists():
        return dst
    vf = []
    if crop:
        x, y, w, h = crop
        vf.append(f"crop={w}:{h}:{x}:{y}")
    vf.append(f"scale=-2:{height}:flags=bicubic")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", ",".join(vf),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-g", "30", "-an",
           "-movflags", "+faststart", str(dst.with_suffix(".tmp.mp4"))]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"documentary proxy failed: {r.stderr.strip()[-600:]}")
    dst.with_suffix(".tmp.mp4").replace(dst)
    return dst


def _slice_curve(curve: MotionCurve, start: float, end: float) -> MotionCurve:
    idx = [i for i, t in enumerate(curve.times) if start <= t <= end]
    sub = MotionCurve(times=[curve.times[i] for i in idx], subject=[curve.subject[i] for i in idx],
                      camera=[curve.camera[i] for i in idx], boxes=[curve.boxes[i] for i in idx])
    finalize_curve(sub)
    return sub


def heuristic_category(shot: BankShot, box_area: float) -> str:
    if shot.max_motion >= 0.25:
        return "hero"
    if box_area >= 0.12 and shot.max_motion >= 0.03:
        return "aura"
    if shot.max_motion < 0.06:
        return "broll"
    return "other"


def score_shot(s: BankShot) -> float:
    motion = min(1.0, s.max_motion / 0.45)
    inten = (s.intensity or 5) / 10.0
    fram = (s.framing or 6) / 10.0
    if s.category == "hero":
        return round(0.5 * motion + 0.3 * inten + 0.2 * fram, 4)
    if s.category == "aura":
        return round(0.6 * fram + 0.25 * (1 - motion) + 0.15 * inten, 4)
    if s.category == "broll":
        return round(0.7 * fram + 0.3 * (1 - motion), 4)
    return round(0.3 * motion + 0.2 * fram, 4)


def analyze_documentary(src: str | Path, duration: float | None = None, animal: str = "auto", progress: Progress | None = None,
                        client: ClaudeClient | None = None, budget: BudgetTracker | None = None, max_classify: int = 1500,
                        force: bool = False) -> dict:
    """Full pipeline with caching. Returns the bank dict (also written to <cache>/bank.json)."""
    src = Path(src)
    cache = cache_dir_for(src)
    timings: dict[str, float] = {}
    bank_path = cache / "bank.json"
    if bank_path.exists() and not force:
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
        if bank.get("animal") == animal or animal == "auto":
            return bank
    info = probe(src)
    duration = duration or info.duration

    def step(p: float, msg: str) -> None:
        if progress:
            progress(p, msg)

    # 1) letterbox
    t0 = time.time()
    crop_json = cache / "crop.json"
    if crop_json.exists():
        bars = json.loads(crop_json.read_text())
    else:
        step(0.01, "detecting letterbox")
        bars = detect_letterbox(src, duration)
        crop_json.write_text(json.dumps(bars))
    rect = crop_rect(bars, info.width, info.height)
    has_crop = rect != (0, 0, info.width, info.height)
    timings["letterbox"] = round(time.time() - t0, 1)

    # 2) proxy
    t0 = time.time()
    step(0.03, "building 540p proxy (once per film)")
    proxy = build_proxy(src, cache / "proxy.mp4", rect if has_crop else None)
    timings["proxy"] = round(time.time() - t0, 1)
    pinfo = probe(proxy)

    # 3) shots
    t0 = time.time()
    shots_json = cache / "shots.json"
    if shots_json.exists():
        shots = [Shot(**d) for d in json.loads(shots_json.read_text())]
    else:
        step(0.12, "shot detection")
        shots = detect_shots_adaptive(proxy, pinfo.duration)
        shots_json.write_text(json.dumps([s.to_dict() for s in shots]))
    timings["shots"] = round(time.time() - t0, 1)

    # 4) motion over the whole proxy
    t0 = time.time()
    motion_json = cache / "motion.json"
    if motion_json.exists():
        curve = MotionCurve.from_dict(json.loads(motion_json.read_text()))
    else:
        step(0.2, "motion scoring")
        curve = compute_motion(proxy)
        motion_json.write_text(json.dumps(curve.to_dict()))
    timings["motion"] = round(time.time() - t0, 1)

    # 5) per-shot keyframes, cheap filters, dedupe, motion stats
    t0 = time.time()
    thumbs = cache / "thumbs"
    thumbs.mkdir(exist_ok=True)
    bank_shots: list[BankShot] = []
    kept_hashes: list[tuple[int, int, int]] = []
    times_arr = np.asarray(curve.times)
    for i, sh in enumerate(shots):
        if i % 25 == 0:
            step(0.3 + 0.3 * i / max(1, len(shots)), f"filtering shots {i}/{len(shots)}")
        bs = BankShot(index=i, start=round(sh.start, 3), end=round(sh.end, 3), duration=round(sh.duration, 3))
        if sh.duration < MIN_SHOT:
            bs.rejected = "short"
            bank_shots.append(bs)
            continue
        kts = [sh.start + 0.15, (sh.start + sh.end) / 2, max(sh.start + 0.15, sh.end - 0.25)]
        frames = extract_frames(proxy, kts, width=480)
        if not frames:
            bs.rejected = "black"
            bank_shots.append(bs)
            continue
        if is_black(frames):
            bs.rejected = "black"
            bank_shots.append(bs)
            continue
        bs.hash_first, bs.hash_mid = dhash(frames[0]), dhash(frames[min(1, len(frames) - 1)])
        dup = next((j for j, hf, hm in kept_hashes if hamming(hf, bs.hash_first) <= 6 and hamming(hm, bs.hash_mid) <= 6), None)
        if dup is not None:
            bs.rejected = "duplicate"
            bs.duplicate_of = dup
            bank_shots.append(bs)
            continue
        if has_text(frames[1:]):
            bs.rejected = "text"
            bank_shots.append(bs)
            continue
        # channel bugs / watermarks are left to Claude's has_text: the static-corner heuristic also
        # fires on locked-off shots with textured corners
        sub = _slice_curve(curve, sh.start + 0.2, sh.end - 0.2) if sh.duration > 0.8 else _slice_curve(curve, sh.start, sh.end)
        bs.max_motion = round(float(max(sub.smoothed)) if sub.smoothed else 0.0, 4)
        bs.peaks = [round(p, 3) for p in sub.peak_times[:4]]
        kept_hashes.append((i, bs.hash_first, bs.hash_mid))
        tp = thumbs / f"{i}.jpg"
        if not tp.exists():
            try:
                thumbnail(proxy, (sh.start + sh.end) / 2, tp, width=320)
            except Exception:
                pass
        bs.thumb = str(tp) if tp.exists() else ""
        # heuristic first; Claude may overwrite
        sel = (times_arr >= sh.start) & (times_arr <= sh.end)
        areas = [b[2] * b[3] for b, keep in zip(curve.boxes, sel) if keep and b]
        bs.category = heuristic_category(bs, float(np.median(areas)) if areas else 0.0)
        bs.classified_by = "heuristic"
        bank_shots.append(bs)
    timings["filters"] = round(time.time() - t0, 1)

    # 6) Claude classification on keyframes (surviving shots, highest motion first)
    t0 = time.time()
    client = client or get_client()
    survivors = [b for b in bank_shots if not b.rejected]
    species_votes: dict[str, float] = {}
    if client.enabled and survivors:
        order = sorted(survivors, key=lambda b: (-b.max_motion, -b.duration))[:max_classify]
        for start in range(0, len(order), SHOTS_PER_CALL):
            batch = order[start:start + SHOTS_PER_CALL]
            step(0.6 + 0.3 * start / max(1, len(order)), f"Claude classifying shots {start}/{len(order)}")
            content: list[dict] = [{"type": "text", "text": f"Target animal: {animal if animal != 'auto' else 'unknown (identify the main animal)'}. {len(batch)} shots, one keyframe each."}]
            for k, b in enumerate(batch):
                mid = (b.start + b.end) / 2
                fr = extract_frames(proxy, [mid], width=448)
                if not fr:
                    continue
                content.append({"type": "text", "text": f"Shot {k} ({b.duration:.1f}s)"})
                content.append(image_block(frame_to_jpeg_b64(fr[0], quality=68)))
            content.append({"type": "text", "text": f"Tag all {len(batch)} shots (indexes 0..{len(batch) - 1})."})
            try:
                res = client.structured(content, DocShotBatch, budget=budget, system=DOC_SYSTEM, note="doc_classify", max_tokens=3000)
            except Exception as e:  # budget or API error: keep heuristics for the rest
                log.warning("documentary classification stopped: %s", e)
                break
            by_idx = {t.index: t for t in res.tags}
            for k, b in enumerate(batch):
                t = by_idx.get(k)
                if t is None:
                    continue
                b.species = t.species.strip().lower()
                b.caption = t.caption.strip()
                b.intensity, b.framing = int(t.intensity), int(t.framing)
                b.classified_by = "claude"
                if t.has_text:
                    b.rejected = "text"
                elif t.has_people:
                    b.rejected = "people"
                else:
                    b.category = t.category
                    if b.species and b.species != "none":
                        species_votes[b.species] = species_votes.get(b.species, 0.0) + b.duration
    timings["classify"] = round(time.time() - t0, 1)
    target = animal
    if animal == "auto" and species_votes:
        target = max(species_votes, key=species_votes.get)
    if target not in ("auto", "") and client.enabled:
        # shots of a different species are context, not the hero
        for b in bank_shots:
            if not b.rejected and b.category in ("hero", "aura") and b.species and b.species not in ("none", target) and b.classified_by == "claude":
                b.category = "other"
    for b in bank_shots:
        if not b.rejected:
            b.score = score_shot(b)
    bank = {"file_key": file_key(src), "path": str(src), "proxy": str(proxy), "crop": {"bars": bars, "rect": list(rect) if has_crop else None},
            "duration": duration, "fps": info.fps, "width": info.width, "height": info.height, "animal": target,
            "requested_animal": animal, "shots": [b.to_dict() for b in bank_shots], "timings": timings,
            "n_shots": len(shots), "n_kept": len([b for b in bank_shots if not b.rejected]),
            "rejected": {r: len([b for b in bank_shots if b.rejected == r]) for r in ("black", "short", "text", "people", "duplicate", "logo")},
            "categories": {c: len([b for b in bank_shots if not b.rejected and b.category == c]) for c in CATEGORIES},
            "classified_by_claude": len([b for b in bank_shots if b.classified_by == "claude"])}
    bank_path.write_text(json.dumps(bank, indent=1), encoding="utf-8")
    step(1.0, "shot bank ready")
    return bank


HERO_SECONDS_PER_EDIT = 26.0      # ~build + post coverage at 1-4 beat cuts
HERO_SHOTS_PER_EDIT = 6
QUALITY_THRESHOLD = 0.3


def estimate_edits(bank: dict, exclude: set[int] | None = None, banned: set[int] | None = None) -> dict:
    exclude = exclude or set()
    banned = banned or set()
    shots = [BankShot.from_dict(d) for d in bank["shots"]]
    hero = [b for b in shots if not b.rejected and b.category == "hero" and b.score >= QUALITY_THRESHOLD and b.index not in exclude and b.index not in banned]
    aura = [b for b in shots if not b.rejected and b.category == "aura" and b.index not in exclude and b.index not in banned]
    broll = [b for b in shots if not b.rejected and b.category == "broll" and b.index not in banned]
    hero_sec = sum(b.duration for b in hero)
    n = int(min(hero_sec / HERO_SECONDS_PER_EDIT, len(hero) / HERO_SHOTS_PER_EDIT))
    n = max(0, min(6, n))
    return {"supported": n, "hero_shots": len(hero), "hero_seconds": round(hero_sec, 1), "aura_shots": len(aura),
            "broll_shots": len(broll), "quality_threshold": QUALITY_THRESHOLD, "hero_seconds_per_edit": HERO_SECONDS_PER_EDIT}

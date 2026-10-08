"""Documentary projects: register a film, run the bank, estimate, generate child edit projects."""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from sqlmodel import Session, select

from wildcut.analysis.moments import ASPECTS, build_moments
from wildcut.analysis.moments import Moment as MomentData
from wildcut.analysis.motion import MotionCurve
from wildcut.analysis.shots import Shot
from wildcut.claude import BudgetTracker, get_client
from wildcut.config import get_settings
from wildcut.db import Clip, Documentary, Project, now
from wildcut.db import Moment as MomentRow
from wildcut.documentary.bank import (
    BankShot,
    analyze_documentary,
    cache_dir_for,
    estimate_edits,
)
from wildcut.documentary.planner import partition_shots
from wildcut.media import MediaError, probe
from wildcut.services.jobs import enqueue
from wildcut.services.planning import plan_project
from wildcut.services.projects import project_dir, write_json

log = logging.getLogger(__name__)


CHAPTER_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?\s*[-\u2013:|]?\s*(.+?)\s*$")


def parse_chapters(text: str, duration: float) -> list[dict]:
    """'0:00 lion', '3:12 - emperor penguin', '1:02:40 iguana' (one per line) -> [{start, end, animal}]."""
    rows = []
    for line in (text or "").splitlines():
        m = CHAPTER_RE.match(line)
        if not m:
            continue
        h, mi, se, frac, animal = m.groups()
        t = (int(h) if h else 0) * 3600 + int(mi) * 60 + int(se) + (float("0." + frac) if frac else 0.0)
        rows.append({"start": round(t, 2), "animal": animal.strip().lower().rstrip(".")})
    rows.sort(key=lambda r: r["start"])
    for i, r in enumerate(rows):
        r["end"] = round(rows[i + 1]["start"], 2) if i + 1 < len(rows) else round(duration, 2)
    return [r for r in rows if r["end"] > r["start"]]


def shot_animal(shot: BankShot, chapters: list[dict]) -> str:
    """The animal a shot belongs to: its chapter when Tim gave timestamps, else Claude's species tag."""
    mid = (shot.start + shot.end) / 2
    for c in chapters or []:
        if c["start"] <= mid < c["end"]:
            return c["animal"]
    return (shot.species or "").strip().lower()


def _same_animal(a: str, b: str) -> bool:
    """'penguin' ~ 'emperor penguin' ~ 'penguin chick' (whole word), 'lion' ~ 'lions'; 'bird' !~ 'lyrebird'."""
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    if a == b or a.rstrip("s") == b.rstrip("s"):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return re.search(r"\b" + re.escape(short) + r"\b", long_) is not None


def animals_in_bank(bank: dict, chapters: list[dict]) -> list[dict]:
    """Per animal: HERO/AURA seconds and shot counts (chapter-based when chapters exist)."""
    shots = [BankShot.from_dict(d) for d in bank["shots"]]
    out: dict[str, dict] = {}
    for b in shots:
        if b.rejected or b.category not in ("hero", "aura"):
            continue
        name = shot_animal(b, chapters)
        if not name or name == "none":
            continue
        row = out.setdefault(name, {"animal": name, "members": [name], "hero_shots": 0, "hero_seconds": 0.0, "aura_shots": 0, "first_seen": b.start})
        if b.category == "hero":
            row["hero_shots"] += 1
            row["hero_seconds"] += b.duration
        else:
            row["aura_shots"] += 1
    rows = list(out.values())
    if not chapters:
        # fold Claude's species variants into one group ('emperor penguin', 'adelie penguin' -> penguin),
        # named after the member with the most HERO seconds; `members` lists every tag in the group
        groups: list[dict] = []
        for r in sorted(rows, key=lambda r: len(r["animal"])):      # shortest names seed the groups
            home = next((g for g in groups if any(_same_animal(m, r["animal"]) for m in g["members"])), None)
            if home is None:
                r["_best"] = r["hero_seconds"]
                groups.append(r)
                continue
            if r["hero_seconds"] > home["_best"]:          # name the group after its strongest single tag
                home["animal"], home["_best"] = r["animal"], r["hero_seconds"]
            home["members"].append(r["animal"])
            home["hero_shots"] += r["hero_shots"]
            home["hero_seconds"] += r["hero_seconds"]
            home["aura_shots"] += r["aura_shots"]
            home["first_seen"] = min(home["first_seen"], r["first_seen"])
        for g in groups:
            g.pop("_best", None)
        rows = groups
    rows = sorted(rows, key=lambda r: r["first_seen"])     # film order, like Tim's timestamps
    for r in rows:
        r["hero_seconds"] = round(r["hero_seconds"], 1)
        r["first_seen"] = round(r["first_seen"], 1)
    return rows


def _members_for(name: str, groups: list[dict]) -> list[str]:
    """Every species tag that belongs with `name` (its group's members), or just the name itself."""
    n = name.strip().lower()
    for g in groups:
        if n == g["animal"] or n in g["members"]:
            return list(g["members"])
    return [n]


def get_documentary(s: Session, project_id: str) -> Documentary | None:
    return s.exec(select(Documentary).where(Documentary.project_id == project_id)).first()


def register(s: Session, project: Project, path: str, animal: str = "auto", edits: str = "as_many") -> Documentary:
    p = Path(path).expanduser()
    if not p.exists():
        # allow a bare file name dropped into inbox/
        cand = get_settings().inbox_dir / path
        if cand.exists():
            p = cand
        else:
            raise MediaError(f"file not found: {path}")
    info = probe(p)
    if not info.has_video:
        raise MediaError("no video stream")
    doc = get_documentary(s, project.id) or Documentary(project_id=project.id, path=str(p))
    doc.path = str(p)
    doc.animal = animal or "auto"
    doc.edits_requested = edits
    doc.duration, doc.fps, doc.width, doc.height = info.duration, info.fps, info.width, info.height
    doc.status = "registered"
    s.add(doc)
    opts = dict(project.options or {})
    opts["documentary"] = {**opts.get("documentary", {}), "path": str(p), "animal": animal, "edits": edits}
    project.options = opts
    s.add(project)
    s.commit()
    s.refresh(doc)
    return doc


def load_bank(doc: Documentary) -> dict | None:
    cache = cache_dir_for(Path(doc.path))
    bp = cache / "bank.json"
    if not bp.exists():
        return None
    return json.loads(bp.read_text(encoding="utf-8"))


def flags(doc: Documentary) -> tuple[set[int], set[int]]:
    return set(doc.starred or []), set(doc.banned or [])


def used_indexes(s: Session, doc: Documentary) -> set[int]:
    used: set[int] = set()
    for pid in doc.edit_project_ids or []:
        for c in s.exec(select(Clip).where(Clip.project_id == pid)).all():
            idx = (c.tags or {}).get("doc_shot_index")
            if idx is not None and (c.tags or {}).get("category") in ("hero", "aura"):
                used.add(int(idx))
    return used


def analyze(s: Session, project: Project, progress: Callable[[float, str], None] | None = None, force: bool = False) -> dict:
    doc = get_documentary(s, project.id)
    if doc is None:
        d = (project.options or {}).get("documentary") or {}
        if not d.get("path"):
            raise MediaError("register a documentary file first")
        doc = register(s, project, d["path"], d.get("animal", "auto"), d.get("edits", "as_many"))
    doc.status = "analyzing"
    s.add(doc)
    s.commit()
    settings = get_settings()
    budget = BudgetTracker(limit_usd=settings.claude_budget_per_documentary_usd, spent_usd=project.claude_spend_usd or 0.0)

    def persist(total: float) -> None:
        project.claude_spend_usd = round(total, 5)
        s.add(project)
        s.commit()

    budget.on_spend = persist
    t0 = time.time()
    bank = analyze_documentary(doc.path, doc.duration, doc.animal, progress, get_client(), budget, force=force)
    doc.analysis_seconds = round(time.time() - t0, 1) if not doc.analysis_seconds or force else doc.analysis_seconds
    doc.proxy_path = bank["proxy"]
    doc.crop = bank["crop"]
    doc.animal_detected = bank.get("animal", "")
    doc.shots_count = bank["n_shots"]
    doc.kept_count = bank["n_kept"]
    doc.timings = bank["timings"]
    starred, banned = flags(doc)
    doc.estimate = estimate_edits(bank, used_indexes(s, doc), banned)
    doc.status = "ready"
    s.add(doc)
    project.status = "analyzed"
    s.add(project)
    s.commit()
    return bank


def bank_view(s: Session, doc: Documentary) -> dict:
    bank = load_bank(doc)
    if bank is None:
        return {"ready": False}
    starred, banned = flags(doc)
    used = used_indexes(s, doc)
    shots = []
    for d in bank["shots"]:
        b = BankShot.from_dict(d)
        shots.append({**d, "starred": b.index in starred, "banned": b.index in banned, "used": b.index in used,
                      "thumb_url": f"/api/media?path={Path(b.thumb).as_posix()}" if b.thumb else None})
    return {"ready": True, "animal": bank.get("animal"), "requested_animal": bank.get("requested_animal"), "proxy_url": f"/api/media?path={Path(bank['proxy']).as_posix()}",
            "crop": bank["crop"], "duration": bank["duration"], "timings": bank["timings"], "n_shots": bank["n_shots"], "n_kept": bank["n_kept"],
            "rejected": bank["rejected"], "categories": bank["categories"], "classified_by_claude": bank.get("classified_by_claude", 0),
            "notes": bank.get("notes", []), "species": bank.get("species", []),
            "animals": animals_in_bank(bank, list(doc.chapters or [])), "chapters": list(doc.chapters or []),
            "estimate": estimate_edits(bank, used, banned), "shots": shots}


def _moments_for_shot(clip: Clip, shot: BankShot, curve: MotionCurve, src_aspect: float) -> list[MomentData]:
    from wildcut.documentary.bank import _slice_curve

    sub = _slice_curve(curve, shot.start, shot.end)
    ms = build_moments(clip.id, [Shot(shot.start, shot.end)], sub, src_aspect, ASPECTS, max_per_clip=4)
    for m in ms:
        m.category = shot.category
        m.species = shot.species or ""
        m.caption_hint = shot.caption
        m.intensity = shot.intensity or max(1, min(10, int(round(m.motion_score * 10))))
        m.framing = shot.framing or 6
        m.subject_visible = shot.category in ("hero", "aura")
        m.action = "sprint" if shot.category == "hero" else "idle"
        from wildcut.analysis.moments import score_moment

        m.score = score_moment(m)
        if shot.category == "hero":
            m.score = round(min(1.0, m.score + 0.2), 4)
    return ms


def generate(s: Session, project: Project, count: int | str, progress: Callable[[float, str], None] | None = None,
             animals: list[str] | None = None) -> list[str]:
    """Create child projects from disjoint HERO/AURA shot sets: `count` edits, or one edit per animal
    in `animals` (shots grouped by Tim's chapters when given, else by Claude's species tag)."""
    doc = get_documentary(s, project.id)
    if doc is None:
        raise MediaError("analyze the documentary first")
    bank = load_bank(doc)
    if bank is None:
        raise MediaError("analyze the documentary first")
    starred, banned = flags(doc)
    used = used_indexes(s, doc)
    est = estimate_edits(bank, used, banned)
    shots = [BankShot.from_dict(d) for d in bank["shots"]]
    chapters = list(doc.chapters or [])
    if animals:
        ok = [b for b in shots if not b.rejected and b.index not in banned]
        groups = animals_in_bank(bank, chapters)
        sets = []
        for name in animals:
            members = _members_for(name, groups)

            def mine_fn(b: BankShot, members: list[str] = members) -> bool:
                sa = shot_animal(b, chapters)
                return sa in members or any(_same_animal(sa, m) for m in members)

            mine = [b for b in ok if mine_fn(b)]
            hero = [b.to_dict() | {"score": b.score + (0.5 if b.index in starred else 0.0)} for b in mine if b.category == "hero"]
            aura = [b.to_dict() | {"score": b.score + (0.5 if b.index in starred else 0.0)} for b in mine if b.category == "aura"]
            # establishing shots only from the same animal: a lion edit never opens on hermit crabs
            broll = [b.to_dict() for b in ok if b.category == "broll" and mine_fn(b)]
            if not hero:
                log.warning("no HERO shots for %s; skipping", name)
                continue
            sets.append({"hero": hero, "aura": aura, "broll": broll, "animal": name})
        if not sets:
            raise MediaError("none of the selected animals has HERO footage")
        n = len(sets)
    else:
        n = est["supported"] if count in ("as_many", "auto", None) else int(count)
        if n <= 0:
            n = 1 if est["hero_shots"] > 0 else 0
        if n == 0:
            raise MediaError("not enough HERO footage for an edit")
        ok = [b for b in shots if not b.rejected and b.index not in banned and b.index not in used]
        hero = [b.to_dict() | {"score": b.score + (0.5 if b.index in starred else 0.0)} for b in ok if b.category == "hero"]
        aura = [b.to_dict() | {"score": b.score + (0.5 if b.index in starred else 0.0)} for b in ok if b.category == "aura"]
        broll = [b.to_dict() for b in shots if not b.rejected and b.category == "broll" and b.index not in banned]
        sets = partition_shots(hero, aura, broll, n)
    curve = MotionCurve.from_dict(json.loads((cache_dir_for(Path(doc.path)) / "motion.json").read_text()))
    pinfo = probe(bank["proxy"])
    src_aspect = pinfo.width / pinfo.height
    d_opts = (project.options or {}).get("documentary") or {}
    animal = bank.get("animal") or doc.animal
    created: list[str] = []
    for k, st in enumerate(sets):
        if progress:
            progress(k / max(1, n), f"building edit {k + 1}/{n}")
        if not st["hero"]:
            continue
        edit_animal = st.get("animal") or animal
        child_name = f"{project.name} — {edit_animal}" if st.get("animal") else f"{project.name} — edit {len(doc.edit_project_ids or []) + k + 1}"
        child = Project(name=child_name, style=project.style if project.style != "showdown" else "phonk",
                        seed=len(doc.edit_project_ids or []) + k + 1,
                        aspect=project.aspect or "9:16", target_length=str(d_opts.get("target", 65)), mode=project.mode,
                        audio_export="silent" if project.audio_export == "original" else project.audio_export, song_path=project.song_path,
                        options={"intensity": (project.options or {}).get("intensity", "med"),
                                 "documentary_edit": {"documentary_id": doc.id, "parent": project.id, "index": k},
                                 "title": f"THE {edit_animal.upper()}" if edit_animal and edit_animal not in ("auto", "none", "", "any") else None})
        s.add(child)
        s.commit()
        s.refresh(child)
        project_dir(child.id)
        chosen = st["hero"] + st["aura"] + st["broll"][:6]
        order = 0
        opener_clip_id = None
        for sd in chosen:
            b = BankShot.from_dict(sd)
            order += 1
            clip = Clip(project_id=child.id, label=f"Clip {order}", order=order, source="documentary", path=doc.path, proxy_path=bank["proxy"],
                        thumb_path=b.thumb or None, duration=b.end, fps=doc.fps, width=doc.width, height=doc.height, has_audio=False,
                        description=b.caption or f"{b.category} shot at {int(b.start // 60)}:{int(b.start % 60):02d}", analyzed=True,
                        window_in=b.start, window_out=b.end, src_crop=(bank["crop"]["rect"] if bank["crop"].get("rect") else None),
                        tags={"species": b.species or (edit_animal if edit_animal not in ("auto", "none", "", "any") else ""), "category": b.category,
                              "doc_shot_index": b.index, "film_time": b.start})
            s.add(clip)
            s.commit()
            s.refresh(clip)
            if b.category == "broll" and opener_clip_id is None:
                opener_clip_id = clip.id          # the rotated partition's first BROLL opens this edit
            for m in _moments_for_shot(clip, b, curve, src_aspect):
                md = m.to_dict()
                s.add(MomentRow(id=m.id, project_id=child.id, clip_id=clip.id, in_t=m.in_t, out_t=m.out_t, peak_t=m.peak_t, motion_score=m.motion_score,
                                species=m.species, action=m.action, intensity=m.intensity, framing=m.framing, subject_visible=m.subject_visible,
                                caption_hint=m.caption_hint, kind=m.kind, category=m.category, crop_paths=md["crop_paths"], score=m.score))
            write_json(child.id, f"{clip.id}.json", {"shots": [{"start": b.start, "end": b.end}], "moments": []})
        s.commit()
        if child.mode == "music" and child.song_path:
            from wildcut.services.analysis import analyze_song

            analyze_song(s, child)
        child.status = "analyzed"
        if opener_clip_id:
            child.options = dict(child.options or {}, opener_clip_id=opener_clip_id)
        s.add(child)
        s.commit()
        row = plan_project(s, child, keep_locks=False, note="documentary plan")
        enqueue(s, child.id, "preview", {"version": row.version})
        created.append(child.id)
    doc.edit_project_ids = list(doc.edit_project_ids or []) + created
    doc.estimate = estimate_edits(bank, used_indexes(s, doc), banned)
    doc.updated_at = now()
    s.add(doc)
    s.commit()
    if progress:
        progress(1.0, f"{len(created)} edit(s) ready")
    return created

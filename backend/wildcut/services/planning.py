"""Planning from the database: build a PlanRequest, run the preset's planner, save the EDL version."""
from __future__ import annotations

import json

from sqlmodel import Session, select

from wildcut.analysis.moments import Moment
from wildcut.db import BeatGrid, Clip, Edl, Moment as MomentRow, Project, latest_edl, save_edl
from wildcut.music.analysis import BeatGridData
from wildcut.planner import plan_for_preset
from wildcut.planner.planner import ClipInfo, PlanRequest
from wildcut.planner.presets import load_preset
from wildcut.services.projects import project_clips, project_dir, read_json

TARGET_LENGTHS = {"15": 15.0, "30": 30.0, "45": 45.0, "60": 60.0, "song": None}


def load_moments(s: Session, project_id: str) -> list[Moment]:
    rows = s.exec(select(MomentRow).where(MomentRow.project_id == project_id)).all()
    out = []
    # shot bounds live in the analysis JSON; fall back to the clip duration
    clips = {c.id: c for c in project_clips(s, project_id)}
    shot_cache: dict[str, list[dict]] = {}
    for r in rows:
        clip = clips.get(r.clip_id)
        if clip is None:
            continue
        if r.clip_id not in shot_cache:
            data = read_json(project_id, f"{r.clip_id}.json") or {}
            shot_cache[r.clip_id] = data.get("shots", [])
        shots = shot_cache[r.clip_id]
        shot = next((sh for sh in shots if sh["start"] - 1e-3 <= r.peak_t <= sh["end"] + 1e-3), None)
        m = Moment(id=r.id, clip_id=r.clip_id, in_t=r.in_t, out_t=r.out_t, peak_t=r.peak_t, motion_score=r.motion_score,
                   species=r.species, action=r.action, intensity=r.intensity, framing=r.framing, subject_visible=r.subject_visible,
                   caption_hint=r.caption_hint, habitat=r.habitat, lighting=r.lighting, dominant_color=r.dominant_color,
                   outcome=r.outcome, crop_paths=dict(r.crop_paths or {}), score=r.score,
                   shot_start=shot["start"] if shot else 0.0, shot_end=shot["end"] if shot else clip.duration, kind=r.kind)
        m.category = r.category or ""
        if r.banned:
            continue
        out.append(m)
    return out


def rows_flag(s: Session, project_id: str, flag: str) -> list[MomentRow]:
    col = getattr(MomentRow, flag)
    return s.exec(select(MomentRow).where(MomentRow.project_id == project_id, col == True)).all()  # noqa: E712


def load_grid(s: Session, project_id: str) -> BeatGridData | None:
    row = s.get(BeatGrid, project_id)
    if row is None:
        return None
    return BeatGridData(tempo=row.tempo, duration=row.duration, beats=list(row.beats or []), downbeats=list(row.downbeats or []),
                        bass_hits=list(row.bass_hits or []), drop_candidates=list(row.drop_candidates or []), chosen_drop=row.chosen_drop,
                        waveform=list(row.waveform or []))


def clip_infos(clips: list[Clip]) -> list[ClipInfo]:
    return [ClipInfo(id=c.id, label=c.label, path=c.path, proxy=c.proxy_path or c.path, duration=c.duration, width=c.width,
                     height=c.height, species=(c.tags or {}).get("species", ""), src_crop=c.src_crop) for c in clips]


def build_request(s: Session, project: Project, seed: int | None = None, existing: dict | None = None) -> PlanRequest:
    clips = project_clips(s, project.id)
    target = TARGET_LENGTHS.get(str(project.target_length), None)
    if target is None and str(project.target_length).replace(".", "").isdigit():
        target = float(project.target_length)
    if project.mode == "visual" and target is None:
        target = 30.0
    opts = dict(project.options or {})
    starred = [r.id for r in rows_flag(s, project.id, "starred")]
    if starred:
        opts["starred_moments"] = sorted(set(opts.get("starred_moments", []) + starred))
    return PlanRequest(
        project_id=project.id, style=project.style, aspect=project.aspect, mode=project.mode, target_length=target,
        seed=seed if seed is not None else project.seed, clips=clip_infos(clips), moments=load_moments(s, project.id),
        grid=load_grid(s, project.id) if project.mode == "music" else None, song_path=project.song_path,
        song_window=project.song_window, audio_export=project.audio_export, intensity=opts.get("intensity"),
        existing=existing, options=opts,
    )


def plan_project(s: Session, project: Project, seed: int | None = None, keep_locks: bool = True, note: str = "plan") -> Edl:
    """Run the planner (honoring locks from the current EDL) and save a new EDL version."""
    existing = None
    cur = current_edl(s, project)
    if keep_locks and cur is not None:
        existing = cur.json
    preset = load_preset(project.style)
    if preset.get("structure") == "showdown":
        edl = plan_showdown_project(s, project, seed, existing)
    elif (project.options or {}).get("documentary_edit"):
        from wildcut.documentary.planner import plan_documentary

        edl = plan_documentary(build_request(s, project, seed, existing))
    else:
        req = build_request(s, project, seed, existing)
        edl = plan_for_preset(req)
    if seed is not None:
        project.seed = seed
    if edl.get("audio", {}).get("song_window"):
        project.song_window = edl["audio"]["song_window"]
    row = save_edl(s, project.id, edl, note=note)
    opts = dict(project.options or {})
    opts["edl_cursor"] = row.version
    project.options = opts
    project.status = "planned"
    s.add(project)
    s.commit()
    (project_dir(project.id) / "edl.json").write_text(json.dumps(row.json, indent=1), encoding="utf-8")
    return row


def plan_showdown_project(s: Session, project: Project, seed: int | None, existing: dict | None) -> dict:
    from wildcut.planner.showdown import plan_showdown

    opts = dict(project.options or {})
    sd = dict(opts.get("showdown") or {})
    clips = {c.id: c for c in project_clips(s, project.id)}
    media = {}
    for r in sd.get("stats", []):
        c = clips.get(r.get("clip_id") or "")
        if c:
            media[r["animal"]] = {"src": c.path, "proxy": c.proxy_path or c.path}
    winner = None
    wid = sd.get("winner_clip_id")
    if wid and wid in clips:
        c = clips[wid]
        moments = [m for m in load_moments(s, project.id) if m.clip_id == wid]
        best = max(moments, key=lambda m: m.score) if moments else None
        winner = {"clip_id": wid, "src": c.path, "proxy": c.proxy_path or c.path,
                  "in": max(0.0, (best.peak_t - 2.0) if best else 0.0), "out": min(c.duration, (best.peak_t + 2.0) if best else c.duration),
                  "moment_id": best.id if best else None, "crop_path": (best.crop_paths.get(project.aspect) if best else None)}
    target = TARGET_LENGTHS.get(str(project.target_length), None)
    return plan_showdown(project.id, project.aspect, project.mode, seed if seed is not None else project.seed, sd,
                         load_grid(s, project.id) if project.mode == "music" else None, project.song_path, project.song_window,
                         project.audio_export, media, winner_clip=winner, intensity=opts.get("intensity"), existing=existing,
                         target_length=target)


def current_edl(s: Session, project: Project) -> Edl | None:
    cursor = (project.options or {}).get("edl_cursor")
    if cursor:
        row = s.exec(select(Edl).where(Edl.project_id == project.id, Edl.version == cursor)).first()
        if row:
            return row
    return latest_edl(s, project.id)


def save_edl_version(s: Session, project: Project, edl: dict, note: str) -> Edl:
    from wildcut.planner import edl as edlmod

    edlmod.relayout(edl)
    row = save_edl(s, project.id, edl, note=note)
    opts = dict(project.options or {})
    opts["edl_cursor"] = row.version
    project.options = opts
    s.add(project)
    s.commit()
    return row


def move_cursor(s: Session, project: Project, delta: int) -> Edl | None:
    rows = s.exec(select(Edl).where(Edl.project_id == project.id).order_by(Edl.version)).all()
    if not rows:
        return None
    versions = [r.version for r in rows]
    cur = (project.options or {}).get("edl_cursor") or versions[-1]
    idx = versions.index(cur) if cur in versions else len(versions) - 1
    idx = max(0, min(len(versions) - 1, idx + delta))
    opts = dict(project.options or {})
    opts["edl_cursor"] = versions[idx]
    project.options = opts
    s.add(project)
    s.commit()
    return rows[idx]

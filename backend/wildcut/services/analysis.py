"""Analysis job: proxies, shots, motion, moments, Claude tags, clip descriptions, beat grid."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from wildcut.analysis.moments import ASPECTS, build_moments
from wildcut.analysis.motion import compute_motion
from wildcut.analysis.shots import detect_shots
from wildcut.analysis.vision import describe_clip, tag_moments
from wildcut.claude import get_client
from wildcut.db import BeatGrid, Clip, Moment as MomentRow, Project
from wildcut.media import make_proxy, probe, thumbnail
from wildcut.music.analysis import analyze_track
from wildcut.services.projects import budget_for, project_clips, project_dir, write_json

log = logging.getLogger(__name__)
Progress = Callable[[float, str], None]


def ensure_proxy(project_id: str, clip: Clip) -> Path:
    proxy = project_dir(project_id) / "proxies" / f"{clip.id}.mp4"
    if clip.proxy_path and Path(clip.proxy_path).exists():
        return Path(clip.proxy_path)
    if not proxy.exists():
        make_proxy(clip.path, proxy)
    return proxy


def analyze_clip(s: Session, project: Project, clip: Clip, progress: Progress | None = None, tag: bool = True) -> list[MomentRow]:
    pid = project.id
    if progress:
        progress(0.0, f"{clip.label}: proxy")
    proxy = ensure_proxy(pid, clip)
    clip.proxy_path = str(proxy)
    thumb = project_dir(pid) / "thumbs" / f"{clip.id}.jpg"
    if not thumb.exists():
        try:
            thumbnail(proxy, min(1.0, clip.duration / 2), thumb)
        except Exception:
            pass
    clip.thumb_path = str(thumb) if thumb.exists() else None
    if progress:
        progress(0.2, f"{clip.label}: shots")
    shots = detect_shots(proxy, clip.duration)
    if progress:
        progress(0.3, f"{clip.label}: motion")
    curve = compute_motion(proxy)
    info = probe(proxy)
    moments = build_moments(clip.id, shots, curve, info.width / info.height, ASPECTS)
    if progress:
        progress(0.7, f"{clip.label}: tagging")
    budget = budget_for(s, project)
    client = get_client()
    if tag:
        tag_moments(proxy, moments, budget=budget, client=client)
        desc = describe_clip(proxy, clip.duration, budget=budget, client=client)
        clip.description = desc.description
        tags = dict(clip.tags or {})
        tags.update({"species": desc.species, "habitat": desc.habitat, "has_people": desc.has_people, "has_text": desc.has_text})
        clip.tags = tags
    # moment thumbnails for the editor's swap list
    for m in moments[:12]:
        mt = project_dir(pid) / "thumbs" / f"m_{m.id}.jpg"
        if not mt.exists():
            try:
                thumbnail(proxy, m.peak_t, mt, width=240)
            except Exception:
                pass
    write_json(pid, f"{clip.id}.json", {"shots": [sh.to_dict() for sh in shots], "motion": curve.to_dict(),
                                        "moments": [m.to_dict() for m in moments]})
    clip.analysis = {"shots": len(shots), "peaks": len(curve.peak_times), "max_motion": max(curve.smoothed or [0.0])}
    clip.analyzed = True
    s.add(clip)
    # replace moment rows for this clip
    for old in s.exec(select(MomentRow).where(MomentRow.clip_id == clip.id)).all():
        s.delete(old)
    rows = []
    for m in moments:
        d = m.to_dict()
        row = MomentRow(id=m.id, project_id=pid, clip_id=clip.id, in_t=m.in_t, out_t=m.out_t, peak_t=m.peak_t,
                        motion_score=m.motion_score, species=m.species, action=m.action, intensity=m.intensity, framing=m.framing,
                        subject_visible=m.subject_visible, caption_hint=m.caption_hint, habitat=m.habitat, lighting=m.lighting,
                        dominant_color=m.dominant_color, outcome=m.outcome, kind=m.kind, crop_paths=d["crop_paths"], score=m.score)
        row.category = m.category or ""
        s.add(row)
        rows.append(row)
    s.commit()
    if progress:
        progress(1.0, f"{clip.label}: done")
    return rows


def analyze_song(s: Session, project: Project, progress: Progress | None = None) -> BeatGrid | None:
    if not project.song_path or not Path(project.song_path).exists():
        return None
    if progress:
        progress(0.0, "music: beats and drop")
    grid = analyze_track(project.song_path)
    data = grid.to_dict()
    write_json(project.id, "beatgrid.json", data)
    row = s.get(BeatGrid, project.id) or BeatGrid(project_id=project.id)
    row.tempo, row.duration = grid.tempo, grid.duration
    row.beats, row.downbeats, row.bass_hits = grid.beats, grid.downbeats, grid.bass_hits
    row.drop_candidates, row.chosen_drop, row.waveform = grid.drop_candidates, grid.chosen_drop, grid.waveform
    s.add(row)
    s.commit()
    if progress:
        progress(1.0, "music: done")
    return row


def analyze_project(s: Session, project: Project, progress: Progress | None = None, only_unanalyzed: bool = True) -> None:
    clips = project_clips(s, project.id)
    todo = [c for c in clips if not (only_unanalyzed and c.analyzed and c.proxy_path and Path(c.proxy_path).exists())]
    has_song = bool(project.mode == "music" and project.song_path)
    n = len(todo) + (1 if has_song else 0)
    done = 0
    # two visible stages: the video (all clips) and the song, each with its own progress
    stages: dict[str, dict] = {}
    if todo:
        stages["video"] = {"progress": 0.0, "message": f"{len(todo)} clip(s) to analyze", "state": "pending"}
    if has_song:
        stages["song"] = {"progress": 0.0, "message": "waiting for the video", "state": "pending"}

    def emit(p: float, msg: str) -> None:
        if not progress:
            return
        try:
            progress(p, msg, stages=stages)
        except TypeError:          # plain (p, msg) callbacks
            progress(p, msg)

    for c in todo:
        def sub(p: float, msg: str, _done=done) -> None:
            stages["video"] = {"progress": round((_done + p) / max(1, len(todo)), 3), "message": msg, "state": "running"}
            emit((_done + p) / max(1, n), msg)
        analyze_clip(s, project, c, sub)
        done += 1
    if todo:
        stages["video"] = {"progress": 1.0, "message": "done", "state": "done"}
    if has_song:
        def sub2(p: float, msg: str) -> None:
            stages["song"] = {"progress": round(p, 3), "message": msg, "state": "done" if p >= 1 else "running"}
            emit((done + p) / max(1, n), msg)
        analyze_song(s, project, sub2)
    project.status = "analyzed"
    project.progress = 1.0
    s.add(project)
    s.commit()

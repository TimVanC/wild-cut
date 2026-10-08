"""FastAPI app: projects, footage, music, analysis, EDL, previews, exports, chat, showdown, media."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from wildcut import __version__
from wildcut.config import get_settings
from wildcut.db import BeatGrid, ChatMessage, Clip, Edl, Export, Job, LibraryClip, Moment as MomentRow, Project, get_session, now
from wildcut.media import MediaError
from wildcut.planner import edl as edlmod
from wildcut.planner.presets import list_presets, load_preset
from wildcut.planner.showdown import blockers, list_layouts
from wildcut.services import edl_ops
from wildcut.services.jobs import enqueue
from wildcut.services.planning import clip_infos, current_edl, load_moments, move_cursor, plan_project, save_edl_version
from wildcut.services.preview import changed_ranges
from wildcut.services.projects import (add_clip_from_library, add_clip_from_path, delete_clip, is_audio_file, project_clips,
                                       project_dir)
from wildcut.stock.search import adapters, expand_queries, prescore, run_search, stock_status

app = FastAPI(title="Wild Cut", version=__version__)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _project(s: Session, project_id: str) -> Project:
    p = s.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


def _media_url(path: str | None) -> str | None:
    if not path:
        return None
    return f"/api/media?path={Path(path).as_posix()}"


def clip_out(c: Clip) -> dict:
    d = c.model_dump()
    d["proxy_url"] = _media_url(c.proxy_path)
    d["thumb_url"] = _media_url(c.thumb_path)
    return d


def project_out(s: Session, p: Project) -> dict:
    d = p.model_dump()
    clips = project_clips(s, p.id)
    d["clip_count"] = len(clips)
    d["thumb_url"] = next((_media_url(c.thumb_path) for c in clips if c.thumb_path), None)
    exp = s.exec(select(Export).where(Export.project_id == p.id).order_by(Export.created_at.desc())).first()
    d["last_export"] = exp.model_dump() if exp else None
    edl = current_edl(s, p)
    d["edl_version"] = edl.version if edl else None
    d["song_url"] = f"/api/projects/{p.id}/song/audio" if p.song_path else None
    d["budget"] = {"spent": p.claude_spend_usd, "limit": get_settings().claude_budget_per_project_usd}
    return d


# ---------------------------------------------------------------- config

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/api/config")
def config() -> dict:
    settings = get_settings()
    return {"presets": list_presets(), "showdown_layouts": list_layouts(), "aspects": list(edlmod.ASPECT_RATIOS.keys())[:4],
            "target_lengths": ["15", "30", "45", "60", "song"], "stock": stock_status(),
            "claude": {"enabled": settings.claude_enabled, "model": settings.claude_model,
                       "budget_per_project_usd": settings.claude_budget_per_project_usd,
                       "needs_workspace": settings.anthropic_api_key.startswith("sk-ant-usr") and not settings.anthropic_workspace_id},
            "inbox_dir": str(settings.inbox_dir), "exports_dir": str(settings.exports_dir),
            "showdown_stats": load_preset("showdown")["showdown"]["stats"]}


# ---------------------------------------------------------------- projects

class ProjectIn(BaseModel):
    name: str
    style: str = "phonk"
    aspect: str | None = None
    target_length: str = "30"
    mode: str = "music"
    audio_export: str = "silent"
    options: dict = {}


class ProjectPatch(BaseModel):
    name: str | None = None
    style: str | None = None
    aspect: str | None = None
    target_length: str | None = None
    mode: str | None = None
    audio_export: str | None = None
    seed: int | None = None
    options: dict | None = None
    song_window: dict | None = None


@app.get("/api/projects")
def list_projects(s: Session = Depends(get_session)) -> list[dict]:
    rows = s.exec(select(Project).order_by(Project.updated_at.desc())).all()
    return [project_out(s, p) for p in rows]


@app.post("/api/projects")
def create_project(body: ProjectIn, s: Session = Depends(get_session)) -> dict:
    preset = load_preset(body.style)
    p = Project(name=body.name.strip() or "Untitled", style=preset["id"], aspect=body.aspect or preset.get("default_aspect", "9:16"),
                target_length=body.target_length, mode=body.mode, audio_export=body.audio_export, options=body.options or {})
    s.add(p)
    s.commit()
    s.refresh(p)
    project_dir(p.id)
    return project_out(s, p)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, s: Session = Depends(get_session)) -> dict:
    return project_out(s, _project(s, project_id))


@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    data = body.model_dump(exclude_none=True)
    if "options" in data:
        opts = dict(p.options or {})
        opts.update(data.pop("options"))
        p.options = opts
    if "style" in data:
        data["style"] = load_preset(data["style"])["id"]
    for k, v in data.items():
        setattr(p, k, v)
    p.updated_at = now()
    s.add(p)
    s.commit()
    s.refresh(p)
    return project_out(s, p)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    for model in (Clip, MomentRow, Edl, Export, Job, ChatMessage):
        for row in s.exec(select(model).where(model.project_id == project_id)).all():
            s.delete(row)
    bg = s.get(BeatGrid, project_id)
    if bg:
        s.delete(bg)
    s.delete(p)
    s.commit()
    shutil.rmtree(get_settings().projects_dir / project_id, ignore_errors=True)
    return {"ok": True}


# ---------------------------------------------------------------- footage

class ClipPathIn(BaseModel):
    path: str
    source: str = "local"


@app.get("/api/projects/{project_id}/clips")
def list_clips(project_id: str, s: Session = Depends(get_session)) -> list[dict]:
    _project(s, project_id)
    return [clip_out(c) for c in project_clips(s, project_id)]


@app.post("/api/projects/{project_id}/clips")
def add_clip(project_id: str, body: ClipPathIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    try:
        c = add_clip_from_path(s, p, body.path, source=body.source)
    except MediaError as e:
        raise HTTPException(400, str(e)) from e
    return clip_out(c)


@app.post("/api/projects/{project_id}/clips/upload")
async def upload_clips(project_id: str, files: list[UploadFile] = File(...), analyze: bool = True,
                       s: Session = Depends(get_session)) -> dict:
    """Drag-and-drop uploads (chat panel / footage screen). Imports, then analyzes in the background."""
    p = _project(s, project_id)
    added = []
    updir = project_dir(p.id) / "uploads"
    for f in files:
        dst = updir / Path(f.filename or "upload.mp4").name
        with dst.open("wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        try:
            c = add_clip_from_path(s, p, dst, source="chat")
        except MediaError as e:
            dst.unlink(missing_ok=True)
            raise HTTPException(400, f"{f.filename}: {e}") from e
        added.append(c)
    job = None
    if analyze and added:
        job = enqueue(s, p.id, "analyze_clips", {"clip_ids": [c.id for c in added]})
    return {"clips": [clip_out(c) for c in added], "job": job.model_dump() if job else None}


@app.delete("/api/projects/{project_id}/clips/{clip_id}")
def remove_clip(project_id: str, clip_id: str, s: Session = Depends(get_session)) -> dict:
    _project(s, project_id)
    c = s.get(Clip, clip_id)
    if c is None or c.project_id != project_id:
        raise HTTPException(404, "clip not found")
    delete_clip(s, c)
    return {"ok": True}


@app.get("/api/projects/{project_id}/clips/{clip_id}/frame")
def clip_frame(project_id: str, clip_id: str, t: float = 0.0, width: int = 320, s: Session = Depends(get_session)) -> Response:
    import cv2

    from wildcut.media import extract_frames

    _project(s, project_id)
    c = s.get(Clip, clip_id)
    if c is None:
        raise HTTPException(404, "clip not found")
    frames = extract_frames(c.proxy_path or c.path, [max(0.0, min(c.duration - 0.05, t))], width=width)
    if not frames:
        raise HTTPException(404, "no frame")
    ok, buf = cv2.imencode(".jpg", frames[0], [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return Response(buf.tobytes(), media_type="image/jpeg")


@app.get("/api/library")
def library(q: str | None = None, s: Session = Depends(get_session)) -> list[dict]:
    from wildcut.stock.library import list_library

    rows = list_library(s, q)
    out = []
    for r in rows:
        d = r.model_dump()
        d["thumb_url"] = _media_url(r.thumb_path)
        out.append(d)
    return out


@app.post("/api/projects/{project_id}/library/{lib_id}")
def add_from_library(project_id: str, lib_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    lib = s.get(LibraryClip, lib_id)
    if lib is None:
        raise HTTPException(404, "library clip not found")
    return clip_out(add_clip_from_library(s, p, lib))


@app.get("/api/inbox")
def inbox_listing() -> dict:
    inbox = get_settings().inbox_dir
    files = [f.name for f in sorted(inbox.iterdir()) if f.is_file() and not f.name.startswith(".")] if inbox.exists() else []
    return {"dir": str(inbox), "files": files}


# ---------------------------------------------------------------- stock

class StockImportIn(BaseModel):
    project_id: str
    results: list[dict]
    then_plan: bool = False


@app.get("/api/stock/search")
def stock_search(q: str, orientation: str | None = None, min_height: int = 720, expand: bool = True, score: bool = True,
                 kind: str = "video", s: Session = Depends(get_session)) -> dict:
    st = stock_status()
    if not st["enabled"]:
        raise HTTPException(400, st["message"])
    from wildcut.planner.chase import pairs_table

    queries = expand_queries(q, chase_pairs=pairs_table(load_preset("chase"))) if expand else [q]
    try:
        results = run_search(queries, orientation, min_height, sources=adapters(), kind=kind)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)) from e
    if score:
        results = prescore(results)
    lib = {f"{r.source}:{r.source_id}": r.id for r in s.exec(select(LibraryClip)).all()}
    out = []
    for r in results:
        d = r.to_dict()
        d["in_library"] = lib.get(r.key)
        out.append(d)
    return {"queries": queries, "results": out}


@app.post("/api/stock/import")
def stock_import(body: StockImportIn, s: Session = Depends(get_session)) -> dict:
    _project(s, body.project_id)
    job = enqueue(s, body.project_id, "import_stock", {"results": body.results, "then_plan": body.then_plan})
    return {"job": job.model_dump()}


# ---------------------------------------------------------------- music

class SongIn(BaseModel):
    path: str


class WindowIn(BaseModel):
    start: float
    end: float
    locked: bool = True


class DropIn(BaseModel):
    t: float


@app.post("/api/projects/{project_id}/song")
def set_song(project_id: str, body: SongIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    path = Path(body.path).expanduser()
    if not path.exists() or not is_audio_file(path):
        raise HTTPException(400, "audio file not found (MP3/WAV/M4A)")
    p.song_path = str(path)
    p.song_window = None
    p.mode = "music"
    p.updated_at = now()
    s.add(p)
    s.commit()
    job = enqueue(s, p.id, "analyze", {"then_plan": False, "only_song": True})
    return {"project": project_out(s, p), "job": job.model_dump()}


@app.post("/api/projects/{project_id}/song/upload")
async def upload_song(project_id: str, file: UploadFile = File(...), s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    dst = project_dir(p.id) / "uploads" / Path(file.filename or "song.mp3").name
    with dst.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    if not is_audio_file(dst):
        dst.unlink(missing_ok=True)
        raise HTTPException(400, "not an audio file")
    return set_song(project_id, SongIn(path=str(dst)), s)


@app.delete("/api/projects/{project_id}/song")
def clear_song(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    p.song_path = None
    p.song_window = None
    p.mode = "visual"
    s.add(p)
    s.commit()
    return project_out(s, p)


@app.get("/api/projects/{project_id}/song/audio")
def song_audio(project_id: str, s: Session = Depends(get_session)) -> FileResponse:
    p = _project(s, project_id)
    if not p.song_path or not Path(p.song_path).exists():
        raise HTTPException(404, "no song")
    return FileResponse(p.song_path)


@app.get("/api/projects/{project_id}/beatgrid")
def beatgrid(project_id: str, s: Session = Depends(get_session)) -> dict:
    _project(s, project_id)
    row = s.get(BeatGrid, project_id)
    if row is None:
        raise HTTPException(404, "song not analyzed yet")
    return row.model_dump()


@app.put("/api/projects/{project_id}/song/window")
def set_window(project_id: str, body: WindowIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    p.song_window = {"start": round(body.start, 3), "end": round(body.end, 3)}
    s.add(p)
    s.commit()
    row = current_edl(s, p)
    if row is not None:
        edl = dict(row.json)
        if body.locked:
            edl_ops.set_song_window(edl, body.start, body.end)
        else:
            edl["audio"]["song_window"] = p.song_window
            edl["audio"]["window_locked"] = False
        save_edl_version(s, p, edl, "song window")
    return project_out(s, p)


@app.put("/api/projects/{project_id}/song/drop")
def set_drop(project_id: str, body: DropIn, s: Session = Depends(get_session)) -> dict:
    _project(s, project_id)
    row = s.get(BeatGrid, project_id)
    if row is None:
        raise HTTPException(404, "song not analyzed yet")
    row.chosen_drop = round(body.t, 4)
    s.add(row)
    s.commit()
    return row.model_dump()


# ---------------------------------------------------------------- analysis and planning

@app.post("/api/projects/{project_id}/analyze")
def analyze(project_id: str, force: bool = False, then_plan: bool = True, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    if not project_clips(s, p.id) and load_preset(p.style).get("structure") != "showdown":
        raise HTTPException(400, "add footage first")
    job = enqueue(s, p.id, "analyze", {"force": force, "then_plan": then_plan})
    return {"job": job.model_dump()}


@app.get("/api/projects/{project_id}/moments")
def moments(project_id: str, clip_id: str | None = None, s: Session = Depends(get_session)) -> list[dict]:
    _project(s, project_id)
    q = select(MomentRow).where(MomentRow.project_id == project_id)
    if clip_id:
        q = q.where(MomentRow.clip_id == clip_id)
    out = []
    thumbs = project_dir(project_id) / "thumbs"
    for m in s.exec(q.order_by(MomentRow.score.desc())).all():
        d = m.model_dump(exclude={"crop_paths"})
        t = thumbs / f"m_{m.id}.jpg"
        d["thumb_url"] = _media_url(str(t)) if t.exists() else None
        out.append(d)
    return out


class MomentFlagIn(BaseModel):
    starred: bool | None = None
    banned: bool | None = None


@app.patch("/api/projects/{project_id}/moments/{moment_id}")
def flag_moment(project_id: str, moment_id: str, body: MomentFlagIn, s: Session = Depends(get_session)) -> dict:
    _project(s, project_id)
    m = s.get(MomentRow, moment_id)
    if m is None:
        raise HTTPException(404, "moment not found")
    if body.starred is not None:
        m.starred = body.starred
    if body.banned is not None:
        m.banned = body.banned
    s.add(m)
    s.commit()
    return m.model_dump(exclude={"crop_paths"})


class PlanIn(BaseModel):
    seed: int | None = None
    keep_locks: bool = True
    text_only: bool = False
    preview: bool = True


@app.post("/api/projects/{project_id}/plan")
def plan(project_id: str, body: PlanIn | None = None, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    body = body or PlanIn()
    if body.text_only:
        row = current_edl(s, p)
        if row is None:
            raise HTTPException(400, "no edit yet")
        edl = dict(row.json)
        note = edl_ops.rebuild_text(edl)
        new = save_edl_version(s, p, edl, note)
    else:
        seed = body.seed if body.seed is not None else (p.seed + 1 if body.keep_locks is not None and body.seed is None and current_edl(s, p) else p.seed)
        try:
            new = plan_project(s, p, seed=seed, keep_locks=body.keep_locks, note="regenerate" if current_edl(s, p) else "plan")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"planning failed: {e}") from e
    job = enqueue(s, p.id, "preview", {"version": new.version}) if body.preview else None
    return {"edl": new.json, "version": new.version, "job": job.model_dump() if job else None}


# ---------------------------------------------------------------- EDL

@app.get("/api/projects/{project_id}/edl")
def get_edl(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    row = current_edl(s, p)
    if row is None:
        raise HTTPException(404, "no edit yet; analyze and plan first")
    versions = s.exec(select(Edl.version).where(Edl.project_id == project_id).order_by(Edl.version)).all()
    return {"edl": row.json, "version": row.version, "note": row.note, "versions": list(versions),
            "can_undo": row.version > min(versions), "can_redo": row.version < max(versions)}


@app.get("/api/projects/{project_id}/edl/versions")
def edl_versions(project_id: str, s: Session = Depends(get_session)) -> list[dict]:
    _project(s, project_id)
    rows = s.exec(select(Edl).where(Edl.project_id == project_id).order_by(Edl.version)).all()
    return [{"version": r.version, "note": r.note, "created_at": r.created_at, "duration": r.json.get("duration")} for r in rows]


class EdlPut(BaseModel):
    edl: dict
    note: str = "manual edit"


@app.put("/api/projects/{project_id}/edl")
def put_edl(project_id: str, body: EdlPut, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    edl = body.edl
    edlmod.relayout(edl)
    errs = edlmod.validate(edl)
    if errs:
        raise HTTPException(400, "; ".join(errs))
    old = current_edl(s, p)
    row = save_edl_version(s, p, edl, body.note)
    return {"edl": row.json, "version": row.version, "changed": changed_ranges(old.json if old else None, row.json)}


class EdlOp(BaseModel):
    op: str
    args: dict[str, Any] = {}
    preview: bool = True


@app.post("/api/projects/{project_id}/edl/op")
def edl_op(project_id: str, body: EdlOp, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    row = current_edl(s, p)
    if row is None:
        raise HTTPException(404, "no edit yet")
    edl = json.loads(json.dumps(row.json))
    a = body.args
    try:
        if body.op == "swap_moment":
            m = next((m for m in load_moments(s, p.id) if m.id == a["moment_id"]), None)
            if m is None:
                raise edl_ops.EdlOpError("unknown moment")
            info = {c.id: c for c in clip_infos(project_clips(s, p.id))}[m.clip_id]
            note = edl_ops.swap_moment(edl, a["item_id"], m, info)
        elif body.op == "set_clip_range":
            note = edl_ops.set_clip_range(edl, a["item_id"], a.get("in"), a.get("out"))
        elif body.op == "set_order":
            note = edl_ops.set_order(edl, a["ids"])
        elif body.op == "set_text":
            note = edl_ops.set_title(edl, a.get("text"), a.get("t"), a.get("duration"))
        elif body.op == "delete_item":
            note = edl_ops.delete_item(edl, a["item_id"])
        elif body.op == "toggle_effect":
            note = edl_ops.toggle_effect(edl, a["item_id"], a.get("enabled"))
        elif body.op == "set_effect_intensity":
            note = edl_ops.set_effect_intensity(edl, a["item_id"], a["intensity"])
        elif body.op == "set_all_intensity":
            note = edl_ops.set_all_intensity(edl, a["intensity"])
        elif body.op == "add_effect":
            e = edl_ops.add_effect(edl, a["type"], a["t"], a.get("intensity"), a.get("duration"))
            note = f"added {e['type']} at {e['t']:.2f}s"
        elif body.op == "set_speed_ramp":
            note = edl_ops.set_speed_ramp(edl, a["item_id"], a.get("slow_rate", 0.4), a.get("peak"))
        elif body.op == "set_lock":
            note = edl_ops.set_lock(edl, a["item_id"], bool(a.get("locked", True)))
        elif body.op == "remove_clip":
            note = edl_ops.remove_clip(edl, a["item_id"])
        elif body.op == "set_enabled":
            found = edlmod.find_item(edl, a["item_id"])
            if not found:
                raise edl_ops.EdlOpError("unknown item")
            found[1]["enabled"] = bool(a.get("enabled", True))
            edlmod.relayout(edl)
            note = "toggled"
        else:
            raise HTTPException(400, f"unknown op {body.op}")
    except (edl_ops.EdlOpError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    new = save_edl_version(s, p, edl, note)
    job = enqueue(s, p.id, "preview", {"version": new.version}) if body.preview else None
    return {"edl": new.json, "version": new.version, "note": note, "changed": changed_ranges(row.json, new.json),
            "job": job.model_dump() if job else None}


@app.post("/api/projects/{project_id}/edl/undo")
def undo(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    row = move_cursor(s, p, -1)
    if row is None:
        raise HTTPException(404, "nothing to undo")
    enqueue(s, p.id, "preview", {"version": row.version})
    return get_edl(project_id, s)


@app.post("/api/projects/{project_id}/edl/redo")
def redo(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    row = move_cursor(s, p, +1)
    if row is None:
        raise HTTPException(404, "nothing to redo")
    enqueue(s, p.id, "preview", {"version": row.version})
    return get_edl(project_id, s)


@app.get("/api/projects/{project_id}/frame")
def edl_frame(project_id: str, t: float = 0.0, width: int = 360, s: Session = Depends(get_session)) -> Response:
    import cv2

    from wildcut.render.composer import render_frame

    p = _project(s, project_id)
    row = current_edl(s, p)
    if row is None:
        raise HTTPException(404, "no edit yet")
    frame = render_frame(row.json, max(0.0, min(t, row.json["duration"] - 0.001)), width=width)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return Response(buf.tobytes(), media_type="image/jpeg")


# ---------------------------------------------------------------- preview and export

@app.post("/api/projects/{project_id}/preview")
def preview(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    if current_edl(s, p) is None:
        raise HTTPException(400, "no edit yet")
    job = enqueue(s, p.id, "preview", {})
    return {"job": job.model_dump()}


@app.get("/api/projects/{project_id}/preview")
def preview_status(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    row = current_edl(s, p)
    if row is None:
        return {"ready": False, "url": None, "version": None}
    path = project_dir(p.id) / "previews" / f"v{row.version}.mp4"
    job = s.exec(select(Job).where(Job.project_id == p.id, Job.kind == "preview", Job.status.in_(["queued", "running"]))).first()
    return {"ready": path.exists(), "url": _media_url(str(path)) if path.exists() else None, "version": row.version,
            "job": job.model_dump() if job else None}


class ExportIn(BaseModel):
    quality: str = "full"
    audio: str | None = None


@app.post("/api/projects/{project_id}/export")
def export(project_id: str, body: ExportIn | None = None, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    body = body or ExportIn()
    row = current_edl(s, p)
    if row is None:
        raise HTTPException(400, "no edit yet")
    if row.json.get("showdown"):
        bad = blockers((p.options or {}).get("showdown", {}).get("stats", []))
        if bad:
            raise HTTPException(400, "Showdown export blocked: confirm or source values for " + ", ".join(bad))
    job = enqueue(s, p.id, "export", {"quality": body.quality, "audio": body.audio})
    return {"job": job.model_dump()}


@app.get("/api/projects/{project_id}/exports")
def exports(project_id: str, s: Session = Depends(get_session)) -> list[dict]:
    _project(s, project_id)
    rows = s.exec(select(Export).where(Export.project_id == project_id).order_by(Export.created_at.desc())).all()
    out = []
    for r in rows:
        d = r.model_dump()
        d["url"] = _media_url(r.path)
        base = Path(r.path).with_suffix("")
        d["credits_path"] = str(base) + "_credits.txt"
        d["caption_path"] = str(base) + "_caption.txt"
        cap = Path(d["caption_path"])
        d["caption"] = cap.read_text(encoding="utf-8") if cap.exists() else ""
        out.append(d)
    return out


@app.post("/api/reveal")
def reveal(body: dict) -> dict:
    """Open the folder containing a path in Finder / Explorer (local-only convenience)."""
    import subprocess
    import sys

    path = Path(body.get("path", ""))
    if not path.exists():
        raise HTTPException(404, "path not found")
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e
    return {"ok": True}


# ---------------------------------------------------------------- showdown

class ShowdownIn(BaseModel):
    stat: str = "top_speed"
    unit: str | None = None
    animals: list[str] = []
    custom_label: str = ""
    layout: str = "default"
    winner_clip_id: str | None = None


class StatsIn(BaseModel):
    stats: list[dict]


@app.get("/api/projects/{project_id}/showdown")
def get_showdown(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    sd = dict((p.options or {}).get("showdown") or {})
    sd["blockers"] = blockers(sd.get("stats", []))
    return sd


@app.put("/api/projects/{project_id}/showdown")
def put_showdown(project_id: str, body: ShowdownIn, draft: bool = True, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    opts = dict(p.options or {})
    sd = dict(opts.get("showdown") or {})
    cfg = load_preset("showdown")["showdown"]["stats"]
    scfg = cfg.get(body.stat) or cfg["custom"]
    sd.update({"stat": body.stat, "unit": body.unit or scfg["default_unit"], "animals": [a.strip().lower() for a in body.animals if a.strip()],
               "custom_label": body.custom_label, "layout": body.layout, "winner_clip_id": body.winner_clip_id})
    opts["showdown"] = sd
    p.options = opts
    p.style = "showdown"
    s.add(p)
    s.commit()
    job = enqueue(s, p.id, "showdown_stats", {}) if draft else None
    return {"showdown": sd, "job": job.model_dump() if job else None}


@app.put("/api/projects/{project_id}/showdown/stats")
def put_stats(project_id: str, body: StatsIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    opts = dict(p.options or {})
    sd = dict(opts.get("showdown") or {})
    rows = []
    for r in body.stats:
        rows.append({"animal": str(r.get("animal", "")).strip().lower(), "value": (float(r["value"]) if r.get("value") not in (None, "") else None),
                     "unit": r.get("unit", sd.get("unit", "")), "source_url": (r.get("source_url") or "").strip(), "fact": r.get("fact", ""),
                     "fact_source_url": r.get("fact_source_url", ""), "confirmed": bool(r.get("confirmed")), "clip_id": r.get("clip_id")})
    sd["stats"] = rows
    opts["showdown"] = sd
    p.options = opts
    s.add(p)
    s.commit()
    return {"showdown": sd, "blockers": blockers(rows)}


# ---------------------------------------------------------------- Director chat

class ChatIn(BaseModel):
    message: str


@app.get("/api/projects/{project_id}/chat")
def chat_history(project_id: str, s: Session = Depends(get_session)) -> list[dict]:
    _project(s, project_id)
    rows = s.exec(select(ChatMessage).where(ChatMessage.project_id == project_id).order_by(ChatMessage.created_at)).all()
    out = []
    for r in rows:
        if r.role == "tool":
            continue
        tools = [b.get("name") for b in (r.blocks or []) if b.get("type") == "tool_use"]
        if r.role == "assistant" and not r.content and not tools:
            continue
        out.append({"id": r.id, "role": r.role, "content": r.content, "tools": tools, "edl_version": r.edl_version, "created_at": r.created_at})
    return out


@app.post("/api/projects/{project_id}/chat")
def chat(project_id: str, body: ChatIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    if not project_clips(s, p.id):
        raise HTTPException(400, "add footage first")
    job = enqueue(s, p.id, "chat", {"message": body.message})
    return {"job": job.model_dump()}


# ---------------------------------------------------------------- jobs, events, media

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, s: Session = Depends(get_session)) -> dict:
    j = s.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return j.model_dump()


@app.get("/api/projects/{project_id}/jobs")
def project_jobs(project_id: str, s: Session = Depends(get_session)) -> list[dict]:
    rows = s.exec(select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())).all()[:20]
    return [j.model_dump() for j in rows]


def _snapshot(s: Session, project_id: str) -> dict:
    p = s.get(Project, project_id)
    if p is None:
        return {}
    jobs = s.exec(select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())).all()[:8]
    edl = current_edl(s, p)
    chat_n = s.exec(select(ChatMessage).where(ChatMessage.project_id == project_id)).all()
    return {"project": {"id": p.id, "status": p.status, "progress": p.progress, "message": p.message, "updated_at": str(p.updated_at),
                        "claude_spend_usd": p.claude_spend_usd, "song_path": p.song_path},
            "jobs": [{"id": j.id, "kind": j.kind, "status": j.status, "progress": j.progress, "message": j.message, "error": j.error,
                      "result": j.result} for j in jobs],
            "edl_version": edl.version if edl else None, "chat_count": len(chat_n)}


@app.get("/api/projects/{project_id}/events")
async def events(project_id: str) -> StreamingResponse:
    """Server-sent events: a snapshot whenever project/job/EDL/chat state changes (polled every 0.5 s)."""
    from wildcut.db import session as make_session

    async def gen():
        last = None
        idle = 0
        while True:
            with make_session() as s:
                snap = _snapshot(s, project_id)
            payload = json.dumps(snap, default=str)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _allowed_media(path: Path) -> bool:
    settings = get_settings()
    roots = [settings.data_dir, settings.exports_dir, settings.inbox_dir]
    try:
        rp = path.resolve()
    except OSError:
        return False
    return any(str(rp).startswith(str(r.resolve())) for r in roots)


@app.get("/api/media")
def media(path: str) -> FileResponse:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "not found")
    if not _allowed_media(p):
        raise HTTPException(403, "path not allowed")
    media_type = "video/mp4" if p.suffix.lower() in (".mp4", ".m4v", ".mov") else None
    return FileResponse(str(p), media_type=media_type)


@app.get("/api/file")
def any_file(path: str) -> FileResponse:
    """Serve a user-provided local footage file for hover previews (local app, single user)."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(str(p))


@app.get("/api/browse")
def browse(path: str | None = None) -> dict:
    """Minimal folder listing so the UI can pick local files by path."""
    base = Path(path).expanduser() if path else Path.home()
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, "folder not found")
    dirs, files = [], []
    try:
        for e in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                dirs.append(e.name)
            elif e.suffix.lower() in {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".mp3", ".wav", ".m4a", ".jpg", ".jpeg", ".png"}:
                files.append({"name": e.name, "size": e.stat().st_size})
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    return {"path": str(base), "parent": str(base.parent) if base.parent != base else None, "dirs": dirs, "files": files}


# documentary routes live in their own module
from wildcut.api import documentary as _documentary_routes  # noqa: E402,F401


# ---------------------------------------------------------------- server mode: access token + built frontend
import os as _os  # noqa: E402
from fastapi import Request as _Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from wildcut.config import REPO_ROOT as _ROOT  # noqa: E402

_ACCESS_TOKEN = _os.environ.get("WILDCUT_ACCESS_TOKEN", "").strip()


@app.middleware("http")
async def _access_token_guard(request: _Request, call_next):
    """When WILDCUT_ACCESS_TOKEN is set (public server), every /api call needs the token
    (header X-Wildcut-Token, ?token=, or the wc_token cookie the UI sets after you enter it)."""
    if _ACCESS_TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/health":
        supplied = request.headers.get("x-wildcut-token") or request.query_params.get("token") or request.cookies.get("wc_token")
        if supplied != _ACCESS_TOKEN:
            return JSONResponse({"detail": "access token required"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth")
def auth_status(request: _Request) -> dict:
    """Lets the UI know whether a token is required (the guard above already validated it if so)."""
    return {"required": bool(_ACCESS_TOKEN), "ok": True}


_DIST = _ROOT / "frontend" / "dist"
if (_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str) -> FileResponse:
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_DIST / "index.html"))

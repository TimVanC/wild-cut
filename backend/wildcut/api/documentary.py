"""Documentary mode endpoints: register, analyze, shot bank, star/ban, estimate, generate edits."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from wildcut.api.app import _project, app, project_out
from wildcut.db import Project, get_session
from wildcut.documentary.bank import estimate_edits
from wildcut.documentary.service import bank_view, flags, get_documentary, load_bank, register, used_indexes
from wildcut.media import MediaError
from wildcut.services.jobs import enqueue
from wildcut.services.projects import project_dir


class RegisterIn(BaseModel):
    path: str
    animal: str = "auto"
    edits: str = "as_many"


class FlagIn(BaseModel):
    starred: bool | None = None
    banned: bool | None = None


class GenerateIn(BaseModel):
    count: str | int = "as_many"


@app.post("/api/projects/{project_id}/documentary/register")
def doc_register(project_id: str, body: RegisterIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    try:
        doc = register(s, p, body.path, body.animal, body.edits)
    except MediaError as e:
        raise HTTPException(400, str(e)) from e
    return doc.model_dump()


@app.post("/api/projects/{project_id}/documentary/upload")
async def doc_upload(project_id: str, file: UploadFile = File(...), animal: str = "auto", edits: str = "as_many",
                     s: Session = Depends(get_session)) -> dict:
    """Upload the film from the browser (streamed to disk in 4 MB chunks) and register it."""
    p = _project(s, project_id)
    dst = project_dir(p.id) / "uploads" / Path(file.filename or "documentary.mp4").name
    with dst.open("wb") as out:
        while chunk := await file.read(4 << 20):
            out.write(chunk)
    try:
        doc = register(s, p, str(dst), animal, edits)
    except MediaError as e:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from e
    return doc.model_dump()


@app.post("/api/projects/{project_id}/documentary/analyze")
def doc_analyze(project_id: str, force: bool = False, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    if get_documentary(s, p.id) is None and not ((p.options or {}).get("documentary") or {}).get("path"):
        raise HTTPException(400, "register a documentary file first")
    job = enqueue(s, p.id, "documentary_analyze", {"force": force})
    return {"job": job.model_dump()}


@app.get("/api/projects/{project_id}/documentary")
def doc_get(project_id: str, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    doc = get_documentary(s, p.id)
    if doc is None:
        return {"registered": False, "options": (p.options or {}).get("documentary") or {}}
    view = bank_view(s, doc) if doc.status == "ready" else {"ready": False}
    edits = []
    for pid in doc.edit_project_ids or []:
        child = s.get(Project, pid)
        if child:
            edits.append(project_out(s, child))
    return {"registered": True, "documentary": doc.model_dump(), "bank": view, "edits": edits}


@app.patch("/api/projects/{project_id}/documentary/shots/{index}")
def doc_flag(project_id: str, index: int, body: FlagIn, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    doc = get_documentary(s, p.id)
    if doc is None:
        raise HTTPException(404, "no documentary")
    starred, banned = flags(doc)
    if body.starred is not None:
        (starred.add if body.starred else starred.discard)(index)
    if body.banned is not None:
        (banned.add if body.banned else banned.discard)(index)
    doc.starred, doc.banned = sorted(starred), sorted(banned)
    bank = load_bank(doc)
    if bank:
        doc.estimate = estimate_edits(bank, used_indexes(s, doc), banned)
    s.add(doc)
    s.commit()
    return {"starred": doc.starred, "banned": doc.banned, "estimate": doc.estimate}


@app.post("/api/projects/{project_id}/documentary/generate")
def doc_generate(project_id: str, body: GenerateIn | None = None, s: Session = Depends(get_session)) -> dict:
    p = _project(s, project_id)
    doc = get_documentary(s, p.id)
    if doc is None or doc.status != "ready":
        raise HTTPException(400, "analyze the documentary first")
    if p.mode == "music" and not p.song_path:
        raise HTTPException(400, "music-synced documentary project: add a song first (Music tab) or switch to visual peaks")
    body = body or GenerateIn()
    job = enqueue(s, p.id, "documentary_generate", {"count": body.count})
    return {"job": job.model_dump()}

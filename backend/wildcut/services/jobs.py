"""Background jobs: a jobs table polled by the worker process, one job at a time."""
from __future__ import annotations

import logging
import traceback
from typing import Callable

from sqlmodel import Session, select

from wildcut.db import Job, Project, now

log = logging.getLogger(__name__)
Handler = Callable[[Session, Job, Callable[[float, str], None]], dict | None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(s: Session, project_id: str, kind: str, payload: dict | None = None) -> Job:
    # collapse duplicate queued jobs of the same kind for the same project (e.g. repeated preview requests)
    for old in s.exec(select(Job).where(Job.project_id == project_id, Job.kind == kind, Job.status == "queued")).all():
        old.status = "cancelled"
        old.updated_at = now()
        s.add(old)
    job = Job(project_id=project_id, kind=kind, payload=payload or {})
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


def claim_next(s: Session) -> Job | None:
    job = s.exec(select(Job).where(Job.status == "queued").order_by(Job.created_at)).first()
    if job is None:
        return None
    job.status = "running"
    job.updated_at = now()
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


def run_job(s: Session, job: Job) -> None:
    fn = HANDLERS.get(job.kind)
    project = s.get(Project, job.project_id) if job.project_id else None

    def progress(p: float, msg: str = "") -> None:
        job.progress = round(float(p), 4)
        job.message = msg or job.message
        job.updated_at = now()
        s.add(job)
        if project is not None:
            project.progress = job.progress
            project.message = f"{job.kind}: {msg}" if msg else project.message
            s.add(project)
        s.commit()

    if fn is None:
        job.status = "error"
        job.error = f"unknown job kind {job.kind}"
        s.add(job)
        s.commit()
        return
    try:
        if project is not None and job.kind in ("analyze", "export", "preview", "plan", "documentary_analyze", "documentary_generate"):
            project.status = {"analyze": "analyzing", "export": "rendering", "preview": "rendering", "plan": "planning",
                              "documentary_analyze": "analyzing", "documentary_generate": "planning"}[job.kind]
            s.add(project)
            s.commit()
        result = fn(s, job, progress) or {}
        job.result = result
        job.status = "done"
        job.progress = 1.0
        job.updated_at = now()
        s.add(job)
        if project is not None:
            project.message = ""
            s.add(project)
        s.commit()
    except Exception as e:  # noqa: BLE001
        log.error("job %s %s failed: %s\n%s", job.id, job.kind, e, traceback.format_exc())
        s.rollback()
        job = s.get(Job, job.id) or job
        job.status = "error"
        job.error = str(e)[:2000]
        job.updated_at = now()
        s.add(job)
        if project is not None:
            project = s.get(Project, project.id) or project
            project.status = "error"
            project.message = f"{job.kind} failed: {str(e)[:300]}"
            s.add(project)
        s.commit()


# ---------------------------------------------------------------- handlers

@handler("analyze")
def _analyze(s: Session, job: Job, progress) -> dict:
    from wildcut.services.analysis import analyze_project

    project = s.get(Project, job.project_id)
    analyze_project(s, project, progress, only_unanalyzed=not job.payload.get("force"))
    if job.payload.get("then_plan", True):
        from wildcut.services.planning import plan_project

        progress(0.98, "planning")
        row = plan_project(s, project, keep_locks=bool(job.payload.get("keep_locks", True)))
        enqueue(s, project.id, "preview", {"version": row.version})
        return {"edl_version": row.version}
    return {}


@handler("plan")
def _plan(s: Session, job: Job, progress) -> dict:
    from wildcut.services.planning import plan_project

    project = s.get(Project, job.project_id)
    row = plan_project(s, project, seed=job.payload.get("seed"), keep_locks=job.payload.get("keep_locks", True),
                       note=job.payload.get("note", "plan"))
    if job.payload.get("then_preview", True):
        enqueue(s, project.id, "preview", {"version": row.version})
    return {"edl_version": row.version}


@handler("preview")
def _preview(s: Session, job: Job, progress) -> dict:
    from wildcut.services.planning import current_edl
    from wildcut.services.preview import render_preview

    project = s.get(Project, job.project_id)
    row = current_edl(s, project)
    if row is None:
        raise ValueError("no EDL to preview")
    out = render_preview(project.id, row.json, row.version, progress)
    project.status = "planned"
    project.message = ""
    s.add(project)
    s.commit()
    return {"path": str(out), "version": row.version}


@handler("export")
def _export(s: Session, job: Job, progress) -> dict:
    from wildcut.services.export import export_project
    from wildcut.services.planning import current_edl

    project = s.get(Project, job.project_id)
    row = current_edl(s, project)
    if row is None:
        raise ValueError("no EDL to export")
    exp = export_project(s, project, row.json, row.version, quality=job.payload.get("quality", "full"),
                         audio=job.payload.get("audio"), progress=progress)
    return {"export_id": exp.id, "path": exp.path, "sound_offset": exp.sound_offset}


@handler("import_stock")
def _import_stock(s: Session, job: Job, progress) -> dict:
    from wildcut.services.projects import add_clip_from_library
    from wildcut.stock.base import StockResult
    from wildcut.stock.library import import_stock
    from wildcut.stock.search import adapters

    project = s.get(Project, job.project_id)
    ads = adapters()
    results = job.payload.get("results", [])
    added = []
    for i, r in enumerate(results):
        progress(i / max(1, len(results)), f"downloading {r.get('source')} {r.get('id')}")
        res = StockResult(**{k: v for k, v in r.items() if k in StockResult.__dataclass_fields__})
        a = ads.get(res.source)
        if a is None:
            continue
        lib = import_stock(s, a, res)
        clip = add_clip_from_library(s, project, lib)
        added.append(clip.id)
    if added and job.payload.get("then_analyze", True):
        enqueue(s, project.id, "analyze", {"then_plan": job.payload.get("then_plan", False)})
    return {"clips": added}


@handler("analyze_clips")
def _analyze_clips(s: Session, job: Job, progress) -> dict:
    """Analyze specific clips (chat uploads) without re-planning."""
    from wildcut.db import Clip
    from wildcut.services.analysis import analyze_clip

    project = s.get(Project, job.project_id)
    ids = job.payload.get("clip_ids", [])
    for i, cid in enumerate(ids):
        clip = s.get(Clip, cid)
        if clip is None:
            continue
        analyze_clip(s, project, clip, lambda p, m, _i=i: progress((_i + p) / max(1, len(ids)), m))
    project.status = "analyzed" if project.status in ("new", "analyzing") else project.status
    s.add(project)
    s.commit()
    return {"clips": ids}


@handler("showdown_stats")
def _showdown_stats(s: Session, job: Job, progress) -> dict:
    from wildcut.planner.showdown import draft_stats
    from wildcut.services.projects import budget_for

    project = s.get(Project, job.project_id)
    opts = dict(project.options or {})
    sd = dict(opts.get("showdown") or {})
    progress(0.1, "drafting stats with Claude")
    rows = draft_stats(sd.get("stat", "top_speed"), sd.get("animals") or None, sd.get("unit", ""), sd.get("custom_label", ""),
                       budget=budget_for(s, project))
    # keep media assignments and confirmations from an earlier sheet
    old = {r["animal"]: r for r in sd.get("stats", [])}
    for r in rows:
        prev = old.get(r["animal"])
        if prev:
            r["clip_id"] = prev.get("clip_id")
    sd["stats"] = rows
    opts["showdown"] = sd
    project.options = opts
    s.add(project)
    s.commit()
    return {"rows": len(rows)}


@handler("chat")
def _chat(s: Session, job: Job, progress) -> dict:
    from wildcut.director.agent import run_turn

    project = s.get(Project, job.project_id)
    result = run_turn(s, project, job.payload.get("message", ""), progress)
    if result.get("edl_changed") and job.payload.get("then_preview", True):
        enqueue(s, project.id, "preview", {})
    return result

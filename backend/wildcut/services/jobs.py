"""Background jobs: a jobs table polled by the worker process, one job at a time."""
from __future__ import annotations

import logging
import traceback
from collections.abc import Callable

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
    job = Job(project_id=project_id, kind=kind, payload=payload or {})
    # collapse duplicate queued jobs of the same kind for the same project (e.g. repeated preview requests),
    # but a song-only analyze must not swallow a queued "analyze and build the edit" (Tim lost his build click)
    for old in s.exec(select(Job).where(Job.project_id == project_id, Job.kind == kind, Job.status == "queued")).all():
        if kind == "analyze" and bool((old.payload or {}).get("only_song")) != bool((payload or {}).get("only_song")):
            continue
        old.status = "cancelled"
        old.updated_at = now()
        s.add(old)
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


STALE_RUNNING_SECONDS = 15 * 60
MAX_RESTARTS = 2


def recover_stale(s: Session, max_age: float = STALE_RUNNING_SECONDS, all_running: bool = False) -> int:
    """Re-queue jobs left "running" by a worker that died (crash, or the old container of a deploy).
    A live job reports progress at least every few minutes; one silent for `max_age` is dead.
    `all_running=True` re-queues every running job (worker startup: the previous worker is gone)."""
    from datetime import timedelta

    rows = s.exec(select(Job).where(Job.status == "running")).all()
    cutoff = now() - timedelta(seconds=max_age)
    n = 0
    for job in rows:
        upd = job.updated_at if job.updated_at.tzinfo else job.updated_at.replace(tzinfo=cutoff.tzinfo)
        if all_running or upd < cutoff:
            restarts = int((job.payload or {}).get("_restarts", 0)) + 1
            job.payload = dict(job.payload or {}, _restarts=restarts)
            if restarts > MAX_RESTARTS:
                # a job that keeps killing the worker is a bug, not bad luck: stop re-running it
                job.status = "error"
                job.error = f"gave up after {restarts - 1} restarts (the worker stopped each time); check the server log"
                job.message = job.error
                p = s.get(Project, job.project_id) if job.project_id else None
                if p is not None:
                    p.status = "error"
                    p.message = f"{job.kind} failed: {job.error}"
                    s.add(p)
            else:
                job.status = "queued"
                job.message = "restarted after the worker stopped"
            job.updated_at = now()
            s.add(job)
            n += 1
    if n:
        s.commit()
    return n


HEAVY_KINDS = {"analyze", "analyze_clips", "documentary_analyze", "documentary_generate", "import_stock", "export"}


def job_lane(job: Job) -> str:
    """'heavy' = minutes of ffmpeg / optical flow / Claude batches; 'light' = seconds (song, plan, preview, chat).
    A song-only analyze job is light so the Music page never waits behind a video."""
    if job.kind == "analyze" and (job.payload or {}).get("only_song"):
        return "light"
    return "heavy" if job.kind in HEAVY_KINDS else "light"


def claim_next(s: Session, lane: str | None = None) -> Job | None:
    """Claim the oldest queued job, optionally only from one lane (two workers run in parallel on the server)."""
    queued = s.exec(select(Job).where(Job.status == "queued").order_by(Job.created_at)).all()
    job = next((j for j in queued if lane is None or job_lane(j) == lane), None)
    if job is None:
        return None
    job.status = "running"
    job.updated_at = now()
    s.add(job)
    s.commit()
    s.refresh(job)
    return job


class JobCancelled(Exception):
    """Raised inside a handler (from its progress callback) when the job was cancelled or its project deleted."""


def cancel_job(s: Session, job_id: str) -> Job | None:
    """Cancel a queued job now, or ask a running one to stop at its next progress tick."""
    job = s.get(Job, job_id)
    if job is None or job.status not in ("queued", "running"):
        return job
    job.status = "cancelled"
    job.message = "cancelled"
    job.updated_at = now()
    s.add(job)
    s.commit()
    s.refresh(job)          # expired after commit: model_dump() would be empty
    return job


def run_job(s: Session, job: Job) -> None:
    fn = HANDLERS.get(job.kind)
    jid, jkind = job.id, job.kind          # plain values: the ORM object is unusable after a failed flush
    pid = job.project_id
    project = s.get(Project, pid) if pid else None

    def progress(p: float, msg: str = "", stages: dict | None = None) -> None:
        # the worker's only chance to notice a Cancel click or a deleted project is here, between steps
        status = s.exec(select(Job.status).where(Job.id == jid)).first()
        if status == "cancelled" or status is None:
            raise JobCancelled("cancelled" if status else "job deleted")
        if pid and s.exec(select(Project.id).where(Project.id == pid)).first() is None:
            raise JobCancelled("project deleted")
        job.progress = round(float(p), 4)
        job.message = msg or job.message
        if stages is not None:
            job.stages = dict(stages)
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
    except JobCancelled as e:
        s.rollback()
        log.info("job %s %s stopped: %s", jid, jkind, e)
        job = s.get(Job, jid)
        if job is not None:
            job.status = "cancelled"
            job.message = f"stopped: {e}"
            job.updated_at = now()
            s.add(job)
            s.commit()
        return
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        s.rollback()                        # first: after a failed flush the session refuses every attribute load
        log.error("job %s %s failed: %s\n%s", jid, jkind, e, tb)
        job = s.get(Job, jid)
        if job is None:
            return
        job.status = "error"
        job.error = str(e)[:2000]
        job.updated_at = now()
        s.add(job)
        pid = job.project_id
        project = s.get(Project, pid) if pid else None
        if project is not None:
            project.status = "error"
            project.message = f"{jkind} failed: {str(e)[:300]}"
            s.add(project)
        s.commit()


# ---------------------------------------------------------------- handlers

@handler("analyze")
def _analyze(s: Session, job: Job, progress) -> dict:
    from wildcut.services.analysis import analyze_project

    project = s.get(Project, job.project_id)
    if job.payload.get("only_song"):
        # a new song never needs the clips re-analyzed
        from wildcut.services.analysis import analyze_song

        def song_progress(p: float, msg: str) -> None:
            progress(p, msg, stages={"song": {"progress": round(p, 3), "message": msg, "state": "done" if p >= 1 else "running"}})

        song_progress(0.05, "music: beats and drop")
        analyze_song(s, project, song_progress)
        song_progress(1.0, "music: done")
        # run_job flipped the project to "analyzing"; a song job does not change the footage state
        from wildcut.services.projects import project_clips

        clips = project_clips(s, project.id)
        project.status = "analyzed" if clips and all(c.analyzed for c in clips) else "new"
        s.add(project)
        s.commit()
        return {}
    analyze_project(s, project, progress, only_unanalyzed=not job.payload.get("force"))
    if job.payload.get("then_plan", True):
        from wildcut.services.planning import plan_project

        progress(0.98, "planning")
        row = plan_project(s, project, keep_locks=bool(job.payload.get("keep_locks", True)))
        brief = str((project.options or {}).get("brief") or "").strip()
        if brief and _brief_pending(s, project):
            # Tim's direction, given before the first edit: the Director applies it to the auto plan
            enqueue(s, project.id, "chat", {"message": "BRIEF:" + chr(10) + brief, "then_preview": True, "brief": True})
        else:
            enqueue(s, project.id, "preview", {"version": row.version})
        return {"edl_version": row.version}
    return {}


def _brief_pending(s: Session, project: Project) -> bool:
    """The brief is applied once, by the first plan, and only when the Director can run."""
    from wildcut.claude import get_client
    from wildcut.db import ChatMessage

    if not get_client().enabled:
        return False
    rows = s.exec(select(ChatMessage).where(ChatMessage.project_id == project.id)).all()
    if any((r.content or "").startswith("BRIEF:") for r in rows):
        return False
    queued = s.exec(select(Job).where(Job.project_id == project.id, Job.kind == "chat")).all()
    return not any((j.payload or {}).get("brief") for j in queued)


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


@handler("documentary_analyze")
def _doc_analyze(s: Session, job: Job, progress) -> dict:
    from wildcut.documentary.service import analyze as doc_analyze

    project = s.get(Project, job.project_id)
    bank = doc_analyze(s, project, progress, force=bool(job.payload.get("force")))
    return {"n_shots": bank["n_shots"], "n_kept": bank["n_kept"], "timings": bank["timings"], "animal": bank.get("animal")}


@handler("documentary_generate")
def _doc_generate(s: Session, job: Job, progress) -> dict:
    from wildcut.documentary.service import generate

    project = s.get(Project, job.project_id)
    ids = generate(s, project, job.payload.get("count", "as_many"), progress, animals=job.payload.get("animals") or None)
    project.status = "planned"
    s.add(project)
    s.commit()
    return {"projects": ids}


@handler("chat")
def _chat(s: Session, job: Job, progress) -> dict:
    from wildcut.director.agent import run_turn

    project = s.get(Project, job.project_id)
    # the brief is the one big turn (it builds the whole edit); ordinary turns stay on the small cap
    result = run_turn(s, project, job.payload.get("message", ""), progress, turn_budget=1.5 if job.payload.get("brief") else None)
    if result.get("edl_changed") and job.payload.get("then_preview", True):
        enqueue(s, project.id, "preview", {})
    return result

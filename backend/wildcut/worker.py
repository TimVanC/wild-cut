"""Worker process: polls the jobs table (one job at a time) and watches the inbox folder.

Run with `python -m wildcut.worker`.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from sqlmodel import Session, select

from wildcut.config import get_settings
from wildcut.db import Project, get_engine
from wildcut.services.jobs import claim_next, enqueue, run_job
from wildcut.services.projects import add_clip_from_path, is_media_file
from wildcut.stock.library import add_local_to_library

log = logging.getLogger("wildcut.worker")
SEEN_FILE = ".wildcut_seen.json"


def _seen(inbox: Path) -> set[str]:
    p = inbox / SEEN_FILE
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(inbox: Path, seen: set[str]) -> None:
    (inbox / SEEN_FILE).write_text(json.dumps(sorted(seen)))


def _stable(path: Path) -> bool:
    """A file still being copied changes size between two polls."""
    try:
        a = path.stat().st_size
        time.sleep(0.4)
        return path.stat().st_size == a and a > 0
    except OSError:
        return False


def watch_inbox(s: Session) -> None:
    inbox = get_settings().inbox_dir
    if not inbox.exists():
        return
    seen = _seen(inbox)
    changed = False
    for entry in sorted(inbox.iterdir()):
        if entry.is_file() and is_media_file(entry) and str(entry) not in seen:
            if not _stable(entry):
                continue
            try:
                add_local_to_library(s, entry, tags={"source": "inbox"})
                log.info("inbox: added %s to the library", entry.name)
            except Exception as e:
                log.warning("inbox: %s failed: %s", entry.name, e)
            seen.add(str(entry))
            changed = True
        elif entry.is_dir():
            project = s.get(Project, entry.name)
            if project is None:
                continue
            new_clips = []
            for f in sorted(entry.iterdir()):
                if f.is_file() and is_media_file(f) and str(f) not in seen:
                    if not _stable(f):
                        continue
                    try:
                        clip = add_clip_from_path(s, project, f, source="inbox")
                        new_clips.append(clip.id)
                    except Exception as e:
                        log.warning("inbox: %s failed: %s", f.name, e)
                    seen.add(str(f))
                    changed = True
            if new_clips:
                enqueue(s, project.id, "analyze", {"then_plan": False})
    if changed:
        _save_seen(inbox, seen)


def main(poll: float = 0.5, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = get_engine()
    get_settings().ensure_dirs()
    log.info("worker started (db=%s)", get_settings().db_path)
    last_inbox = 0.0
    while True:
        with Session(engine) as s:
            # recover jobs left "running" by a crashed worker
            job = claim_next(s)
            if job is not None:
                log.info("job %s %s start", job.id, job.kind)
                run_job(s, job)
                log.info("job %s %s %s", job.id, job.kind, job.status)
                continue
            if time.time() - last_inbox > 2.0:
                try:
                    watch_inbox(s)
                except Exception as e:
                    log.warning("inbox watch failed: %s", e)
                last_inbox = time.time()
        if once:
            return
        time.sleep(poll)


def run_pending(engine=None) -> int:
    """Run every queued job synchronously (tests and the API's in-process fallback)."""
    engine = engine or get_engine()
    n = 0
    with Session(engine) as s:
        while True:
            job = claim_next(s)
            if job is None:
                break
            run_job(s, job)
            n += 1
    return n


if __name__ == "__main__":
    main()

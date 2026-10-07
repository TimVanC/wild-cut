"""Project folders, clip import (local paths, uploads, library), labels, and the Claude budget."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from sqlmodel import Session, select

from wildcut.claude import BudgetTracker
from wildcut.config import get_settings
from wildcut.db import Clip, LibraryClip, Project, now
from wildcut.media import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS, MediaError, image_to_video, probe

SUBDIRS = ("proxies", "analysis", "previews", "renders", "thumbs", "uploads", "chunks")


def project_dir(project_id: str) -> Path:
    d = get_settings().projects_dir / project_id
    for sub in SUBDIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def write_json(project_id: str, name: str, data: dict | list) -> Path:
    p = project_dir(project_id) / "analysis" / name
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return p


def read_json(project_id: str, name: str):
    p = project_dir(project_id) / "analysis" / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def budget_for(s: Session, project: Project) -> BudgetTracker:
    settings = get_settings()

    def persist(total: float) -> None:
        project.claude_spend_usd = round(total, 5)
        s.add(project)
        s.commit()

    return BudgetTracker(limit_usd=settings.claude_budget_per_project_usd, spent_usd=project.claude_spend_usd or 0.0, on_spend=persist)


def next_label(s: Session, project_id: str) -> tuple[str, int]:
    rows = s.exec(select(Clip).where(Clip.project_id == project_id)).all()
    n = max((c.order for c in rows), default=0) + 1
    return f"Clip {n}", n


def add_clip_from_path(s: Session, project: Project, path: str | Path, source: str = "local", copy: bool = False,
                       source_url: str | None = None, credit: str | None = None, license: str | None = None,
                       tags: dict | None = None) -> Clip:
    path = Path(path).expanduser()
    if not path.exists():
        raise MediaError(f"file not found: {path}")
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        video = project_dir(project.id) / "uploads" / f"{path.stem}_still.mp4"
        image_to_video(path, video, duration=6.0)
        path = video
    elif ext not in VIDEO_EXTS:
        raise MediaError(f"unsupported file type: {path.suffix}")
    if copy:
        dst = project_dir(project.id) / "uploads" / path.name
        if dst != path:
            shutil.copy2(path, dst)
        path = dst
    info = probe(path)
    if not info.has_video:
        raise MediaError(f"no video stream in {path}")
    label, order = next_label(s, project.id)
    clip = Clip(project_id=project.id, label=label, order=order, source=source, path=str(path), duration=info.duration,
                fps=info.fps, width=info.width, height=info.height, has_audio=info.has_audio, source_url=source_url,
                credit=credit, license=license, tags=tags or {})
    s.add(clip)
    project.updated_at = now()
    if project.status in ("analyzed", "planned", "exported"):
        project.status = "new"   # new footage needs analysis
    s.add(project)
    s.commit()
    s.refresh(clip)
    return clip


def add_clip_from_library(s: Session, project: Project, lib: LibraryClip) -> Clip:
    return add_clip_from_path(s, project, lib.path, source=lib.source, source_url=lib.source_url, credit=lib.credit,
                              license=lib.license, tags=dict(lib.tags or {}))


def project_clips(s: Session, project_id: str) -> list[Clip]:
    return s.exec(select(Clip).where(Clip.project_id == project_id).order_by(Clip.order)).all()


def delete_clip(s: Session, clip: Clip) -> None:
    from wildcut.db import Moment

    for m in s.exec(select(Moment).where(Moment.clip_id == clip.id)).all():
        s.delete(m)
    s.delete(clip)
    s.commit()


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS

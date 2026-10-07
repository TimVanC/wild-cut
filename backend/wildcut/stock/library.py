"""Local footage library (data/library/): every imported stock clip is kept with its tags and license
so later projects can reuse it without re-downloading."""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, select

from wildcut.config import get_settings
from wildcut.db import LibraryClip
from wildcut.media import image_to_video, probe, thumbnail
from wildcut.stock.base import StockAdapter, StockResult


def find_in_library(s: Session, source: str, source_id: str) -> LibraryClip | None:
    return s.exec(select(LibraryClip).where(LibraryClip.source == source, LibraryClip.source_id == source_id)).first()


def import_stock(s: Session, adapter: StockAdapter, result: StockResult) -> LibraryClip:
    """Download (once) into the library and return the row."""
    existing = find_in_library(s, result.source, result.id)
    if existing and Path(existing.path).exists():
        return existing
    settings = get_settings()
    lib = settings.library_dir
    lib.mkdir(parents=True, exist_ok=True)
    path = adapter.download(result, lib)
    if result.kind == "photo":
        video = lib / f"{result.source}_{result.id}_still.mp4"
        image_to_video(path, video, duration=6.0)
        path = video
    info = probe(path)
    thumb = lib / f"{result.source}_{result.id}.jpg"
    try:
        thumbnail(path, min(1.0, info.duration / 2), thumb)
    except Exception:
        thumb = None
    row = existing or LibraryClip(source=result.source, source_id=result.id)
    row.path = str(path)
    row.thumb_path = str(thumb) if thumb else None
    row.duration = info.duration
    row.width, row.height = info.width, info.height
    row.source_url = result.page_url
    row.credit = result.credit
    row.license = f"{result.license} ({result.license_url})"
    row.tags = {"query": result.query, "tags": result.tags, "score": result.score, "kind": result.kind}
    row.query = result.query
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def add_local_to_library(s: Session, path: Path, tags: dict | None = None) -> LibraryClip:
    settings = get_settings()
    lib = settings.library_dir
    lib.mkdir(parents=True, exist_ok=True)
    dst = lib / path.name
    if not dst.exists():
        shutil.copy2(path, dst)
    info = probe(dst)
    thumb = lib / f"{dst.stem}.jpg"
    try:
        thumbnail(dst, min(1.0, info.duration / 2), thumb)
    except Exception:
        thumb = None
    row = LibraryClip(source="local", source_id=path.name, path=str(dst), thumb_path=str(thumb) if thumb else None,
                      duration=info.duration, width=info.width, height=info.height, tags=tags or {})
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def list_library(s: Session, query: str | None = None) -> list[LibraryClip]:
    rows = s.exec(select(LibraryClip).order_by(LibraryClip.created_at.desc())).all()
    if query:
        q = query.lower()
        rows = [r for r in rows if q in (r.query or "").lower() or any(q in t.lower() for t in (r.tags or {}).get("tags", []))]
    return rows

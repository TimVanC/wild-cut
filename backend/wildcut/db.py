"""SQLite storage via SQLModel. JSON-shaped fields are stored as JSON columns."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import JSON, Column, event
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

from wildcut.config import get_settings


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Project(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    style: str = "phonk"              # preset id: phonk | cinematic | chase | showdown
    aspect: str = "9:16"              # 9:16 | 1:1 | 4:5 | 3:4
    target_length: str = "30"         # "15" | "30" | "45" | "60" | "song"
    mode: str = "music"               # music | visual
    audio_export: str = "silent"      # silent | mixed | original
    song_path: str | None = None
    song_window: dict | None = Field(default=None, sa_column=Column(JSON))
    seed: int = 1
    status: str = "new"               # new | analyzing | analyzed | planned | rendering | exported | error
    progress: float = 0.0
    message: str = ""
    options: dict = Field(default_factory=dict, sa_column=Column(JSON))  # preset-specific (chase variant, showdown config, title override)
    claude_spend_usd: float = 0.0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Clip(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(index=True)
    label: str = ""                   # "Clip 1", "Clip 2", ... in import order
    source: str = "local"             # local | pexels | pixabay | inbox | chat
    path: str
    proxy_path: str | None = None
    thumb_path: str | None = None
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    source_url: str | None = None
    credit: str | None = None
    license: str | None = None
    tags: dict = Field(default_factory=dict, sa_column=Column(JSON))   # species, description, habitat...
    analysis: dict = Field(default_factory=dict, sa_column=Column(JSON))  # shots, motion summary paths
    description: str = ""
    analyzed: bool = False
    order: int = 0
    created_at: datetime = Field(default_factory=now)


class Moment(SQLModel, table=True):
    id: str = Field(primary_key=True)
    project_id: str = Field(index=True)
    clip_id: str = Field(index=True)
    in_t: float
    out_t: float
    peak_t: float
    motion_score: float = 0.0
    species: str = ""
    action: str = ""
    intensity: int = 0
    framing: int = 0
    subject_visible: bool | None = None
    caption_hint: str = ""
    habitat: str = ""
    lighting: str = ""
    dominant_color: str = ""
    outcome: str = ""
    kind: str = "peak"
    crop_paths: dict = Field(default_factory=dict, sa_column=Column(JSON))
    score: float = 0.0


class BeatGrid(SQLModel, table=True):
    project_id: str = Field(primary_key=True)
    tempo: float = 0.0
    duration: float = 0.0
    beats: list = Field(default_factory=list, sa_column=Column(JSON))
    downbeats: list = Field(default_factory=list, sa_column=Column(JSON))
    bass_hits: list = Field(default_factory=list, sa_column=Column(JSON))
    drop_candidates: list = Field(default_factory=list, sa_column=Column(JSON))
    chosen_drop: float | None = None
    waveform: list = Field(default_factory=list, sa_column=Column(JSON))


class Edl(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: str = Field(index=True)
    version: int = 1
    json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    note: str = ""
    created_at: datetime = Field(default_factory=now)


class Export(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(index=True)
    path: str
    settings: dict = Field(default_factory=dict, sa_column=Column(JSON))
    sound_offset: float | None = None
    edl_version: int = 0
    created_at: datetime = Field(default_factory=now)


class Job(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    project_id: str = Field(index=True)
    kind: str                           # analyze | plan | preview | export | import_stock | showdown_stats | ...
    status: str = "queued"              # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))
    error: str = ""
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ChatMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: str = Field(index=True)
    role: str                           # user | assistant | tool
    content: str = ""
    blocks: list = Field(default_factory=list, sa_column=Column(JSON))  # raw API content blocks for replay
    edl_version: int | None = None
    created_at: datetime = Field(default_factory=now)


class LibraryClip(SQLModel, table=True):
    """Imported clips kept across projects (data/library/)."""
    id: str = Field(default_factory=new_id, primary_key=True)
    source: str
    source_id: str = ""
    path: str
    thumb_path: str | None = None
    duration: float = 0.0
    width: int = 0
    height: int = 0
    source_url: str | None = None
    credit: str | None = None
    license: str | None = None
    tags: dict = Field(default_factory=dict, sa_column=Column(JSON))
    query: str = ""
    created_at: datetime = Field(default_factory=now)


_engine: Engine | None = None


def get_engine(path: Path | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.ensure_dirs()
        db_path = path or settings.db_path
        _engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        SQLModel.metadata.create_all(_engine)
    return _engine


def reset_engine_for_tests(path: Path) -> Engine:
    global _engine
    _engine = None
    return get_engine(path)


def session() -> Session:
    return Session(get_engine())


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as s:
        yield s


def latest_edl(s: Session, project_id: str) -> Edl | None:
    return s.exec(select(Edl).where(Edl.project_id == project_id).order_by(Edl.version.desc())).first()


def save_edl(s: Session, project_id: str, data: dict[str, Any], note: str = "") -> Edl:
    cur = latest_edl(s, project_id)
    version = (cur.version + 1) if cur else 1
    data = dict(data)
    data["version"] = version
    row = Edl(project_id=project_id, version=version, json=data, note=note)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row

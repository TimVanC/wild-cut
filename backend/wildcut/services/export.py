"""Export bundle: MP4 (H.264, AAC when audio), credits.txt, caption.txt, sound offset, stats.csv."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field
from sqlmodel import Session

from wildcut.claude import get_client
from wildcut.config import get_settings
from wildcut.db import Export, Project
from wildcut.render.composer import RenderSettings, render
from wildcut.services.projects import budget_for, project_clips

log = logging.getLogger(__name__)


class Caption(BaseModel):
    caption: str = Field(description="one or two short lines, no hashtags, no hype slogans")
    hashtags: list[str] = Field(description="6 to 10 hashtags without the # sign")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "edit"


def fmt_offset(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def write_credits(s: Session, project: Project, edl: dict, path: Path) -> Path:
    clips = {c.id: c for c in project_clips(s, project.id)}
    used_ids = []
    for c in edl.get("clips", []):
        if c.get("clip_id") and c["clip_id"] not in used_ids:
            used_ids.append(c["clip_id"])
    sd = edl.get("showdown") or {}
    for a in sd.get("animals", []):
        for c in clips.values():
            if c.path == a.get("src") and c.id not in used_ids:
                used_ids.append(c.id)
    lines = [f"Wild Cut credits for '{project.name}'", ""]
    stock = 0
    for cid in used_ids:
        c = clips.get(cid)
        if not c:
            continue
        if c.source in ("pexels", "pixabay"):
            stock += 1
            lines.append(f"- {c.label}: {c.credit or c.source} | {c.source_url or ''} | {c.license or ''}")
        else:
            lines.append(f"- {c.label}: local file {Path(c.path).name}")
    if stock == 0:
        lines.append("(no stock footage used)")
    lines += ["", "Fonts: Cinzel, Cormorant Garamond, Anton, Bebas Neue (SIL Open Font License)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_caption(s: Session, project: Project, edl: dict, path: Path, sound_offset: float | None) -> Path:
    title = edl.get("title") or project.name
    species = (title or "").replace("THE ", "").strip().lower()
    client = get_client()
    caption, tags = None, None
    if client.enabled:
        try:
            res = client.structured(
                f"Short-form wildlife edit titled '{title}' ({project.style} style, {edl.get('duration', 0):.0f}s). "
                f"Write a post caption and hashtags.", Caption, budget=budget_for(s, project), note="caption", max_tokens=300)
            caption, tags = res.caption, res.hashtags
        except Exception as e:
            log.warning("caption fell back: %s", e)
    if caption is None:
        caption = f"{title.title() if title else 'Wild Cut'}."
        base = [species.replace(" ", ""), "wildlife", "animals", "nature", "phonk" if project.style in ("phonk", "chase") else "cinematic",
                "edit", "fyp", "wildcut"]
        tags = [t for t in base if t]
    lines = [caption.strip(), "", " ".join(f"#{t.lstrip('#')}" for t in tags)]
    if sound_offset is not None:
        lines += ["", f"Sound offset: start the sound at {fmt_offset(sound_offset)} ({sound_offset:.2f}s into the track)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export_project(s: Session, project: Project, edl: dict, version: int, quality: str = "full",
                   audio: str | None = None, progress: Callable[[float, str], None] | None = None) -> Export:
    settings = get_settings()
    out_dir = settings.exports_dir / f"{_slug(project.name)}_{project.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{_slug(project.name)}_v{version}_{stamp}"
    edl = dict(edl)
    edl["audio"] = dict(edl.get("audio", {}))
    if audio:
        edl["audio"]["export"] = audio
    if edl["audio"].get("export") == "mixed" and not edl["audio"].get("song_path"):
        edl["audio"]["song_path"] = project.song_path
    if edl.get("showdown"):
        from wildcut.planner.showdown import blockers

        rows = (project.options or {}).get("showdown", {}).get("stats", [])
        bad = blockers(rows)
        if bad:
            raise ValueError("Showdown export blocked: unsourced or unconfirmed values for " + ", ".join(bad))
    mp4 = out_dir / f"{name}.mp4"

    def prog(p: float) -> None:
        if progress:
            progress(p * 0.95, "rendering")

    render(edl, mp4, RenderSettings(quality=quality), progress=prog)
    sound_offset = None
    if project.mode == "music" and edl["audio"].get("export") == "silent":
        sound_offset = edl["audio"].get("sound_offset")
        if sound_offset is None and edl["audio"].get("song_window"):
            sound_offset = edl["audio"]["song_window"]["start"]
    write_credits(s, project, edl, out_dir / f"{name}_credits.txt")
    write_caption(s, project, edl, out_dir / f"{name}_caption.txt", sound_offset)
    if edl.get("showdown"):
        from wildcut.planner.showdown import write_stats_csv

        write_stats_csv((project.options or {}).get("showdown", {}).get("stats", []), edl["showdown"].get("label", "stat"),
                        out_dir / f"{name}_stats.csv")
    row = Export(project_id=project.id, path=str(mp4), settings={"quality": quality, "audio": edl["audio"].get("export"),
                                                                  "aspect": edl["aspect"], "duration": edl.get("duration")},
                 sound_offset=sound_offset, edl_version=version)
    s.add(row)
    project.status = "exported"
    s.add(project)
    s.commit()
    s.refresh(row)
    if progress:
        progress(1.0, "export ready")
    return row

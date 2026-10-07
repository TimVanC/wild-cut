"""Settings loaded from .env / environment. Paths resolve relative to the repo root."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _path(env: str, default: str) -> Path:
    raw = os.environ.get(env) or default
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


class Settings(BaseModel):
    anthropic_api_key: str = ""
    anthropic_workspace_id: str = ""   # required by user-scoped keys (sk-ant-usr-...)
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    claude_budget_per_project_usd: float = 1.50
    data_dir: Path = REPO_ROOT / "data"
    inbox_dir: Path = REPO_ROOT / "inbox"
    exports_dir: Path = REPO_ROOT / "exports"
    presets_dir: Path = REPO_ROOT / "presets"
    assets_dir: Path = REPO_ROOT / "assets"
    api_port: int = 8787
    frontend_port: int = 5173

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wildcut.db"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def stock_enabled(self) -> bool:
        return bool(self.pexels_api_key and self.pixabay_api_key)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.inbox_dir, self.exports_dir, self.library_dir, self.projects_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID", ""),
        pexels_api_key=os.environ.get("PEXELS_API_KEY", ""),
        pixabay_api_key=os.environ.get("PIXABAY_API_KEY", ""),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
        claude_budget_per_project_usd=float(os.environ.get("CLAUDE_BUDGET_PER_PROJECT_USD", "1.50")),
        data_dir=_path("DATA_DIR", "./data"),
        inbox_dir=_path("INBOX_DIR", "./inbox"),
        exports_dir=_path("EXPORTS_DIR", "./exports"),
        api_port=int(os.environ.get("API_PORT", "8787")),
        frontend_port=int(os.environ.get("FRONTEND_PORT", "5173")),
    )

"""Stock adapter interface. Only official APIs of licensed libraries; never scraping."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import httpx


@dataclass
class StockResult:
    source: str                 # pexels | pixabay
    id: str
    width: int
    height: int
    duration: float
    thumbnail_url: str
    preview_url: str            # small rendition for hover previews
    file_url: str               # best rendition to download
    page_url: str
    credit: str
    license: str
    license_url: str
    query: str = ""
    tags: list[str] = field(default_factory=list)
    kind: str = "video"         # video | photo
    score: float | None = None  # Claude thumbnail pre-score (1..10)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def orientation(self) -> str:
        if self.width == self.height:
            return "square"
        return "portrait" if self.height > self.width else "landscape"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["orientation"] = self.orientation
        return d


class StockAdapter(Protocol):
    name: str

    def search(self, query: str, orientation: str | None = None, min_height: int = 720, per_page: int = 15,
               kind: str = "video") -> list[StockResult]: ...

    def download(self, result: StockResult, dst_dir: Path) -> Path: ...

    def license_info(self, result: StockResult) -> dict: ...


class StockError(RuntimeError):
    pass


def make_http(transport: httpx.BaseTransport | None = None, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=30.0, headers=headers or {}, transport=transport, follow_redirects=True)


def download_file(http: httpx.Client, url: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with http.stream("GET", url) as r:
        if r.status_code != 200:
            raise StockError(f"download failed ({r.status_code}): {url}")
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
    tmp.replace(dst)
    return dst

"""Pexels Video/Photo API adapter (https://www.pexels.com/api/)."""
from __future__ import annotations

from pathlib import Path

import httpx

from wildcut.stock.base import StockError, StockResult, download_file, make_http

API = "https://api.pexels.com"
LICENSE = "Pexels License"
LICENSE_URL = "https://www.pexels.com/license/"


class PexelsAdapter:
    name = "pexels"

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise StockError("PEXELS_API_KEY is missing")
        self.http = make_http(transport, headers={"Authorization": api_key})

    def search(self, query: str, orientation: str | None = None, min_height: int = 720, per_page: int = 15,
               kind: str = "video") -> list[StockResult]:
        params = {"query": query, "per_page": per_page}
        if orientation in ("portrait", "landscape", "square"):
            params["orientation"] = orientation
        if kind == "photo":
            r = self.http.get(f"{API}/v1/search", params=params)
        else:
            params["size"] = "medium"
            r = self.http.get(f"{API}/videos/search", params=params)
        if r.status_code != 200:
            raise StockError(f"Pexels search failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        out: list[StockResult] = []
        if kind == "photo":
            for p in data.get("photos", []):
                if p.get("height", 0) < min_height:
                    continue
                src = p.get("src", {})
                out.append(StockResult(
                    source="pexels", id=str(p["id"]), width=int(p.get("width", 0)), height=int(p.get("height", 0)), duration=0.0,
                    thumbnail_url=src.get("medium") or src.get("small", ""), preview_url=src.get("large") or src.get("original", ""),
                    file_url=src.get("large2x") or src.get("original", ""), page_url=p.get("url", ""),
                    credit=f"Photo by {p.get('photographer', 'Unknown')} on Pexels", license=LICENSE, license_url=LICENSE_URL,
                    query=query, kind="photo", tags=[p.get("alt", "")] if p.get("alt") else []))
            return out
        for v in data.get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type", "").startswith("video/mp4")]
            if not files:
                continue
            files.sort(key=lambda f: (f.get("height") or 0) * (f.get("width") or 0), reverse=True)
            best = files[0]
            if (best.get("height") or 0) < min_height and (best.get("width") or 0) < min_height:
                continue
            # prefer 1080p-ish for download: huge 4K files are slow and unnecessary for 1080 output
            dl = next((f for f in files if 1000 <= (f.get("height") or 0) <= 1200 or 1000 <= (f.get("width") or 0) <= 1200), best)
            small = files[-1]
            user = v.get("user", {})
            out.append(StockResult(
                source="pexels", id=str(v["id"]), width=int(v.get("width", 0)), height=int(v.get("height", 0)),
                duration=float(v.get("duration", 0)), thumbnail_url=v.get("image", ""), preview_url=small.get("link", ""),
                file_url=dl.get("link", ""), page_url=v.get("url", ""),
                credit=f"Video by {user.get('name', 'Unknown')} on Pexels ({user.get('url', '')})",
                license=LICENSE, license_url=LICENSE_URL, query=query))
        return out

    def download(self, result: StockResult, dst_dir: Path) -> Path:
        ext = ".jpg" if result.kind == "photo" else ".mp4"
        return download_file(self.http, result.file_url, Path(dst_dir) / f"pexels_{result.id}{ext}")

    def license_info(self, result: StockResult) -> dict:
        return {"license": LICENSE, "license_url": LICENSE_URL, "credit": result.credit, "page_url": result.page_url}

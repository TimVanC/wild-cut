"""Pixabay Video/Image API adapter (https://pixabay.com/api/docs/)."""
from __future__ import annotations

from pathlib import Path

import httpx

from wildcut.stock.base import StockError, StockResult, download_file, make_http

API = "https://pixabay.com/api"
LICENSE = "Pixabay Content License"
LICENSE_URL = "https://pixabay.com/service/license-summary/"


class PixabayAdapter:
    name = "pixabay"

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise StockError("PIXABAY_API_KEY is missing")
        self.key = api_key
        self.http = make_http(transport)

    def search(self, query: str, orientation: str | None = None, min_height: int = 720, per_page: int = 15,
               kind: str = "video") -> list[StockResult]:
        params = {"key": self.key, "q": query, "per_page": max(3, min(200, per_page * 2)), "safesearch": "true"}
        if kind == "photo":
            params["image_type"] = "photo"
            if orientation in ("horizontal", "vertical"):
                params["orientation"] = orientation
            elif orientation == "portrait":
                params["orientation"] = "vertical"
            elif orientation == "landscape":
                params["orientation"] = "horizontal"
            r = self.http.get(f"{API}/", params=params)
        else:
            params["video_type"] = "film"
            params["min_height"] = min_height
            r = self.http.get(f"{API}/videos/", params=params)
        if r.status_code != 200:
            raise StockError(f"Pixabay search failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        out: list[StockResult] = []
        if kind == "photo":
            for h in data.get("hits", []):
                if h.get("imageHeight", 0) < min_height:
                    continue
                out.append(StockResult(
                    source="pixabay", id=str(h["id"]), width=int(h.get("imageWidth", 0)), height=int(h.get("imageHeight", 0)), duration=0.0,
                    thumbnail_url=h.get("previewURL", ""), preview_url=h.get("webformatURL", ""),
                    file_url=h.get("largeImageURL") or h.get("webformatURL", ""), page_url=h.get("pageURL", ""),
                    credit=f"Image by {h.get('user', 'Unknown')} on Pixabay", license=LICENSE, license_url=LICENSE_URL,
                    query=query, kind="photo", tags=[t.strip() for t in (h.get("tags") or "").split(",") if t.strip()]))
            return out[:per_page]
        for h in data.get("hits", []):
            vids = h.get("videos", {})
            large = vids.get("large") or vids.get("medium") or {}
            medium = vids.get("medium") or large
            small = vids.get("small") or vids.get("tiny") or medium
            w, hgt = int(large.get("width", 0)), int(large.get("height", 0))
            if hgt < min_height and w < min_height:
                continue
            # orientation is not a video search parameter on Pixabay; filter client-side
            if orientation == "portrait" and not hgt > w:
                continue
            if orientation == "landscape" and not w > hgt:
                continue
            if orientation == "square" and w != hgt:
                continue
            dl = medium if 1000 <= int(medium.get("height", 0)) <= 1200 or 1000 <= int(medium.get("width", 0)) <= 1200 else large
            thumb = medium.get("thumbnail") or small.get("thumbnail") or f"https://i.vimeocdn.com/video/{h.get('picture_id', '')}_295x166.jpg"
            out.append(StockResult(
                source="pixabay", id=str(h["id"]), width=w, height=hgt, duration=float(h.get("duration", 0)),
                thumbnail_url=thumb, preview_url=small.get("url", ""), file_url=dl.get("url", ""), page_url=h.get("pageURL", ""),
                credit=f"Video by {h.get('user', 'Unknown')} on Pixabay", license=LICENSE, license_url=LICENSE_URL,
                query=query, tags=[t.strip() for t in (h.get("tags") or "").split(",") if t.strip()]))
        return out[:per_page]

    def download(self, result: StockResult, dst_dir: Path) -> Path:
        ext = ".jpg" if result.kind == "photo" else ".mp4"
        return download_file(self.http, result.file_url, Path(dst_dir) / f"pixabay_{result.id}{ext}")

    def license_info(self, result: StockResult) -> dict:
        return {"license": LICENSE, "license_url": LICENSE_URL, "credit": result.credit, "page_url": result.page_url}

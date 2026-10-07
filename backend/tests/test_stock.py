"""Stock adapters against mocked HTTP; search helper fallbacks; library import."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from wildcut.claude import ClaudeClient
from wildcut.stock.base import StockError
from wildcut.stock.pexels import PexelsAdapter
from wildcut.stock.pixabay import PixabayAdapter
from wildcut.stock.search import expand_queries, run_search, stock_status

PEXELS_JSON = {
    "videos": [
        {"id": 101, "width": 1920, "height": 1080, "duration": 12, "url": "https://www.pexels.com/video/101/",
         "image": "https://images.pexels.com/101.jpg", "user": {"name": "Ann", "url": "https://www.pexels.com/@ann"},
         "video_files": [
             {"id": 1, "quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://cdn/101_4k.mp4"},
             {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://cdn/101_hd.mp4"},
             {"id": 3, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "link": "https://cdn/101_sd.mp4"}]},
        {"id": 102, "width": 640, "height": 360, "duration": 5, "url": "u", "image": "i", "user": {"name": "B"},
         "video_files": [{"id": 4, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "link": "l"}]},
    ]
}
PIXABAY_JSON = {
    "hits": [
        {"id": 201, "pageURL": "https://pixabay.com/videos/201/", "duration": 20, "tags": "cheetah, savanna", "user": "Cal",
         "videos": {"large": {"url": "https://cdn/201_l.mp4", "width": 3840, "height": 2160, "thumbnail": "https://cdn/201_l.jpg"},
                    "medium": {"url": "https://cdn/201_m.mp4", "width": 1920, "height": 1080, "thumbnail": "https://cdn/201_m.jpg"},
                    "small": {"url": "https://cdn/201_s.mp4", "width": 960, "height": 540, "thumbnail": "https://cdn/201_s.jpg"}}},
        {"id": 202, "pageURL": "p", "duration": 9, "tags": "bird", "user": "D",
         "videos": {"large": {"url": "https://cdn/202_l.mp4", "width": 1080, "height": 1920, "thumbnail": "t"},
                    "medium": {"url": "https://cdn/202_m.mp4", "width": 720, "height": 1280, "thumbnail": "t"},
                    "small": {"url": "https://cdn/202_s.mp4", "width": 360, "height": 640, "thumbnail": "t"}}},
    ]
}


def _transport():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.pexels.com/videos/search" in url:
            assert request.headers.get("Authorization") == "pk"
            assert "orientation=landscape" in url
            return httpx.Response(200, json=PEXELS_JSON)
        if "pixabay.com/api/videos" in url:
            assert "key=xk" in url
            return httpx.Response(200, json=PIXABAY_JSON)
        if url.endswith(".mp4"):
            return httpx.Response(200, content=b"\x00" * 100)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_pexels_search_filters_and_picks_1080p():
    a = PexelsAdapter("pk", transport=_transport())
    res = a.search("cheetah running", orientation="landscape", min_height=720)
    assert [r.id for r in res] == ["101"], "the 360p clip must be filtered out"
    r = res[0]
    assert r.file_url.endswith("101_hd.mp4"), "download the ~1080p rendition, not 4K"
    assert r.preview_url.endswith("101_sd.mp4")
    assert "Ann" in r.credit and r.license == "Pexels License" and r.page_url.startswith("https://www.pexels.com")
    assert r.orientation == "landscape"
    info = a.license_info(r)
    assert info["license_url"].startswith("https://www.pexels.com/license")


def test_pixabay_search_orientation_filter_and_credit():
    a = PixabayAdapter("xk", transport=_transport())
    res = a.search("cheetah", orientation="landscape", min_height=720)
    assert [r.id for r in res] == ["201"]
    assert res[0].file_url.endswith("201_m.mp4") and "cheetah" in res[0].tags and "Cal" in res[0].credit
    portrait = a.search("cheetah", orientation="portrait", min_height=720)
    assert [r.id for r in portrait] == ["202"]


def test_run_search_dedupes_and_merges():
    sources = {"pexels": PexelsAdapter("pk", transport=_transport()), "pixabay": PixabayAdapter("xk", transport=_transport())}
    res = run_search(["cheetah running", "cheetah hunting"], "landscape", 720, sources=sources)
    keys = [r.key for r in res]
    assert len(keys) == len(set(keys)) == 2
    assert {r.source for r in res} == {"pexels", "pixabay"}


def test_missing_keys_disable_stock_with_message():
    from wildcut.config import Settings

    st = stock_status(Settings(pexels_api_key="", pixabay_api_key="x"))
    assert not st["enabled"] and "PEXELS_API_KEY" in st["message"]
    with pytest.raises(StockError):
        PexelsAdapter("")


def test_expand_queries_fallback_without_claude():
    qs = expand_queries("cheetah hunting", client=ClaudeClient(api_key="", model="x"), chase_pairs=[("cheetah", "gazelle")])
    assert qs[0] == "cheetah hunting" and len(qs) <= 8 and len(set(qs)) == len(qs)
    assert any("gazelle" in q for q in qs)


def test_download_and_library_import(db, tmp_path, monkeypatch):
    from wildcut import db as dbmod
    from wildcut.config import get_settings
    from wildcut.stock.library import find_in_library, import_stock, list_library

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    a = PexelsAdapter("pk", transport=_transport())
    r = a.search("cheetah", orientation="landscape")[0]
    # the mocked download is not a real video: patch probe/thumbnail so the library bookkeeping is exercised
    import wildcut.stock.library as lib

    monkeypatch.setattr(lib, "probe", lambda p: type("I", (), {"duration": 12.0, "width": 1920, "height": 1080})())
    monkeypatch.setattr(lib, "thumbnail", lambda *a, **k: None)
    with dbmod.session() as s:
        row = import_stock(s, a, r)
        assert Path(row.path).exists() and row.credit and "Pexels" in row.license
        again = import_stock(s, a, r)
        assert again.id == row.id, "second import reuses the library copy"
        assert find_in_library(s, "pexels", "101") is not None
        assert list_library(s, "cheetah")[0].id == row.id

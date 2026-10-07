"""Search helper: Claude expands a theme into concrete queries, both sources run, results are
de-duplicated and pre-scored by thumbnail with Claude vision so the best action clips sort first."""
from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from wildcut.claude import BudgetTracker, ClaudeClient, get_client, image_block
from wildcut.config import get_settings
from wildcut.stock.base import StockAdapter, StockError, StockResult

log = logging.getLogger(__name__)


class QueryList(BaseModel):
    queries: list[str] = Field(description="5 to 8 concrete stock-footage search queries, 2 to 4 words each")


class ThumbScore(BaseModel):
    index: int
    score: int = Field(ge=1, le=10, description="how likely this is a strong animal action clip (sharp subject, visible animal, movement)")
    animal: str = Field(description="main animal visible, lowercase, or 'none'")


class ThumbScores(BaseModel):
    scores: list[ThumbScore]


SEARCH_SYSTEM = "You write stock footage search queries for a wildlife short-form video editor. Return concrete, specific queries."

ACTION_MODIFIERS = ["running", "hunting", "jumping", "slow motion", "close up", "chase", "fight", "diving"]


def adapters(settings=None, transport=None) -> dict[str, StockAdapter]:
    """Both adapters, or an empty dict with a reason in `stock_status()` when keys are missing."""
    from wildcut.stock.pexels import PexelsAdapter
    from wildcut.stock.pixabay import PixabayAdapter

    settings = settings or get_settings()
    out: dict[str, StockAdapter] = {}
    if settings.pexels_api_key:
        out["pexels"] = PexelsAdapter(settings.pexels_api_key, transport=transport)
    if settings.pixabay_api_key:
        out["pixabay"] = PixabayAdapter(settings.pixabay_api_key, transport=transport)
    return out


def stock_status(settings=None) -> dict:
    settings = settings or get_settings()
    missing = [k for k, v in (("PEXELS_API_KEY", settings.pexels_api_key), ("PIXABAY_API_KEY", settings.pixabay_api_key)) if not v]
    return {"enabled": not missing, "missing": missing,
            "message": "" if not missing else f"Stock search is disabled: add {' and '.join(missing)} to .env."}


def expand_queries(theme: str, client: ClaudeClient | None = None, budget: BudgetTracker | None = None,
                   chase_pairs: list[tuple[str, str]] | None = None) -> list[str]:
    client = client or get_client()
    theme = theme.strip()
    if client.enabled:
        try:
            hint = ""
            if chase_pairs:
                hint = " Include predator and prey queries from these pairs where relevant: " + ", ".join(f"{a}/{b}" for a, b in chase_pairs[:6]) + "."
            res = client.structured(f"Theme: {theme}.{hint} Return 5 to 8 queries.", QueryList, budget=budget,
                                    system=SEARCH_SYSTEM, note="search_expand", max_tokens=400)
            qs = [q.strip() for q in res.queries if q.strip()]
            if qs:
                return _dedupe_queries([theme] + qs)[:8]
        except Exception as e:
            log.warning("query expansion fell back: %s", e)
    base = theme.split()[0] if theme else "animal"
    qs = [theme]
    if chase_pairs:
        for a, b in chase_pairs[:2]:
            qs += [f"{a} chasing {b}", f"{b} running"]
    qs += [f"{base} {m}" for m in ACTION_MODIFIERS]
    return _dedupe_queries(qs)[:8]


def _dedupe_queries(qs: list[str]) -> list[str]:
    seen, out = set(), []
    for q in qs:
        k = q.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out


def run_search(queries: list[str], orientation: str | None, min_height: int, per_query: int = 10,
               sources: dict[str, StockAdapter] | None = None, kind: str = "video") -> list[StockResult]:
    sources = sources if sources is not None else adapters()
    if not sources:
        raise StockError(stock_status()["message"] or "no stock sources configured")
    tasks = [(name, a, q) for q in queries for name, a in sources.items()]
    results: list[StockResult] = []

    def one(item):
        name, a, q = item
        try:
            return a.search(q, orientation=orientation, min_height=min_height, per_page=per_query, kind=kind)
        except Exception as e:
            log.warning("%s search '%s' failed: %s", name, q, e)
            return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(one, tasks):
            results.extend(res)
    seen: set[str] = set()
    unique = []
    for r in results:
        if r.key in seen:
            continue
        seen.add(r.key)
        unique.append(r)
    return unique


def prescore(results: list[StockResult], client: ClaudeClient | None = None, budget: BudgetTracker | None = None,
             http=None, max_items: int = 36) -> list[StockResult]:
    """Score thumbnails 1..10 with Claude vision (12 per call); unscored items keep None and sort last."""
    client = client or get_client()
    if not client.enabled or not results:
        return results
    import httpx

    http = http or httpx.Client(timeout=20.0, follow_redirects=True)
    subset = results[:max_items]
    for start in range(0, len(subset), 12):
        batch = subset[start:start + 12]
        content: list[dict] = []
        ok_idx = []
        for i, r in enumerate(batch):
            try:
                resp = http.get(r.thumbnail_url)
                if resp.status_code != 200 or not resp.content:
                    continue
                media = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                if media not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    media = "image/jpeg"
                content.append({"type": "text", "text": f"Thumbnail {i}"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                            "data": base64.standard_b64encode(resp.content).decode("ascii")}})
                ok_idx.append(i)
            except Exception:
                continue
        if not ok_idx:
            continue
        content.append({"type": "text", "text": "Score each thumbnail for use in a wildlife action edit."})
        try:
            res = client.structured(content, ThumbScores, budget=budget, system=SEARCH_SYSTEM, note="prescore", max_tokens=800)
            for s in res.scores:
                if 0 <= s.index < len(batch):
                    batch[s.index].score = float(s.score)
                    if s.animal and s.animal != "none":
                        batch[s.index].tags = list(dict.fromkeys(batch[s.index].tags + [s.animal]))
        except Exception as e:
            log.warning("prescore fell back: %s", e)
            break
    results.sort(key=lambda r: (r.score is None, -(r.score or 0), -r.duration))
    return results

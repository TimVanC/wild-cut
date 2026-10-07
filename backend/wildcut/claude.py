"""Claude client wrapper: structured outputs, vision batches, chat turns, and a per-project budget.

Every call reports its estimated cost through a `BudgetTracker`, which raises `BudgetExceeded`
once a project has spent its `CLAUDE_BUDGET_PER_PROJECT_USD`. When no API key is configured the
client reports `enabled=False` and callers fall back to heuristics (see analysis/vision.py).
Tests inject a fake via `set_client_for_tests`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Type, TypeVar

from pydantic import BaseModel

from wildcut.config import get_settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Unknown models fall back to the Sonnet rate.
PRICING = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}
CACHE_READ_FRACTION = 0.1

T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    pass


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0) -> float:
    inp, out = PRICING.get(model, (3.0, 15.0))
    return (max(0, input_tokens - cache_read) * inp + cache_read * inp * CACHE_READ_FRACTION + output_tokens * out) / 1e6


@dataclass
class BudgetTracker:
    limit_usd: float
    spent_usd: float = 0.0
    on_spend: Callable[[float], None] | None = None   # persists the running total
    calls: int = 0
    log_lines: list[str] = field(default_factory=list)

    def check(self, reserve_usd: float = 0.0) -> None:
        if self.spent_usd + reserve_usd > self.limit_usd:
            raise BudgetExceeded(f"Claude budget exhausted: ${self.spent_usd:.3f} of ${self.limit_usd:.2f} spent")

    def add(self, cost: float, note: str = "") -> None:
        self.spent_usd += cost
        self.calls += 1
        self.log_lines.append(f"${cost:.4f} {note}")
        if self.on_spend:
            self.on_spend(self.spent_usd)

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)


def image_block(jpeg_b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64}}


class ClaudeClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.model = model or settings.claude_model
        self.workspace_id = settings.anthropic_workspace_id
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _sdk(self):
        if self._client is None:
            import anthropic

            headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
            self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=3, timeout=120.0, default_headers=headers)
        return self._client

    def _usage_cost(self, response: Any) -> float:
        u = getattr(response, "usage", None)
        if u is None:
            return 0.0
        return estimate_cost(self.model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                             getattr(u, "cache_read_input_tokens", 0) or 0)

    def structured(self, content: list[dict] | str, schema: Type[T], budget: BudgetTracker | None = None,
                   system: str | None = None, max_tokens: int = 4000, note: str = "") -> T:
        """One user turn -> validated pydantic object via the Messages API structured outputs."""
        if not self.enabled:
            raise RuntimeError("Claude is not configured (ANTHROPIC_API_KEY missing)")
        if budget:
            budget.check(reserve_usd=0.02)
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=max_tokens,
                                      messages=[{"role": "user", "content": content}], output_format=schema)
        if system:
            kwargs["system"] = system
        response = self._sdk().messages.parse(**kwargs)
        cost = self._usage_cost(response)
        if budget:
            budget.add(cost, note or schema.__name__)
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError("Claude returned no parseable output")
        return parsed

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             budget: BudgetTracker | None = None, max_tokens: int = 4000, note: str = "chat") -> Any:
        """Raw Messages API turn (used by the Director agent loop). Returns the response object."""
        if not self.enabled:
            raise RuntimeError("Claude is not configured (ANTHROPIC_API_KEY missing)")
        if budget:
            budget.check(reserve_usd=0.02)
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        response = self._sdk().messages.create(**kwargs)
        if budget:
            budget.add(self._usage_cost(response), note)
        return response


_override: ClaudeClient | None = None


def get_client() -> ClaudeClient:
    return _override or ClaudeClient()


def set_client_for_tests(client: ClaudeClient | None) -> None:
    global _override
    _override = client

"""Claude vision tagging of candidate moments and clip descriptions.

For the top N candidate peaks we send 3 to 4 frames each, several moments per request, and ask
for a JSON schema: species, action, intensity, framing, subject visibility, plus the habitat /
lighting / color fields Chase pairing needs, an outcome field for the Escape variant, and a
documentary category (hero / aura / broll / other). Calls stay inside the per-project budget.

Without an API key the heuristic fallback tags from motion alone so the rest of the pipeline
still runs (species "animal", action by motion strength).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from wildcut.analysis.moments import ACTIONS, Moment, score_moment
from wildcut.claude import BudgetTracker, ClaudeClient, get_client, image_block
from wildcut.media import extract_frames, frame_to_jpeg_b64

log = logging.getLogger(__name__)

MAX_TAGGED_PER_CLIP = 40
MOMENTS_PER_CALL = 4
FRAMES_PER_MOMENT = 4
FRAME_WIDTH = 512

Action = Literal["pounce", "strike", "leap", "swing", "fight", "catch", "dive", "sprint", "escape", "idle"]
Category = Literal["hero", "aura", "broll", "other"]


class MomentTag(BaseModel):
    index: int = Field(description="index of the moment in the request, starting at 0")
    species: str = Field(description="common name of the main animal, lowercase, e.g. 'cheetah', 'gibbon'; 'none' if no animal")
    action: Action
    intensity: int = Field(ge=1, le=10, description="how intense/dramatic the action is")
    framing: int = Field(ge=1, le=10, description="composition and subject size quality for a vertical social edit")
    subject_visible: bool = Field(description="the animal is clearly visible and identifiable")
    caption_hint: str = Field(description="under 12 words describing what happens, e.g. 'gibbon lets go of the branch'")
    habitat: str = Field(description="one or two words: savanna, forest, river, ocean, snow, desert, jungle, sky, unknown")
    lighting: str = Field(description="one word: golden, daylight, overcast, night, dusk, unknown")
    dominant_color: str = Field(description="one word: green, brown, yellow, blue, grey, white, orange, unknown")
    outcome: Literal["caught", "escaped", "unclear", "none"] = Field(description="for predator/prey moments")
    category: Category = Field(description="hero = animal doing something; aura = animal close-up/posing; broll = landscape with no clear animal; other = a different animal or people")


class MomentTagBatch(BaseModel):
    tags: list[MomentTag]


class ClipDescription(BaseModel):
    species: str = Field(description="main animal common name, lowercase; 'none' if no animal")
    description: str = Field(description="under 12 words, e.g. 'gibbon swinging left to right, low angle'")
    habitat: str = ""
    has_people: bool = Field(description="people, presenters, or crew visible")
    has_text: bool = Field(description="burned-in text, subtitles, logos, or title cards")


VISION_SYSTEM = (
    "You tag wildlife footage for a short-form video editor. Answer only with the requested JSON. "
    "Be literal about what is visible. Use 'idle' when the animal is not doing anything notable."
)


def _moment_frames(proxy_path: str | Path, m: Moment) -> list[str]:
    span = max(0.3, m.out_t - m.in_t)
    ts = [m.in_t + 0.1, m.peak_t - 0.25 * span, m.peak_t, min(m.out_t - 0.05, m.peak_t + 0.3)]
    ts = sorted(set(round(max(m.in_t, min(m.out_t, t)), 3) for t in ts))[:FRAMES_PER_MOMENT]
    frames = extract_frames(proxy_path, ts, width=FRAME_WIDTH)
    return [frame_to_jpeg_b64(f, quality=72) for f in frames]


def heuristic_tags(m: Moment) -> None:
    """No-Claude fallback: tag from motion strength only."""
    m.species = m.species or "animal"
    if m.motion_score >= 0.6:
        m.action = "sprint"
    elif m.motion_score >= 0.25:
        m.action = "leap"
    else:
        m.action = "idle"
    m.intensity = max(1, min(10, int(round(m.motion_score * 10))))
    m.framing = 6
    m.subject_visible = True
    m.category = "hero" if m.action != "idle" else "aura"
    m.score = score_moment(m)


def tag_moments(proxy_path: str | Path, moments: list[Moment], budget: BudgetTracker | None = None,
                client: ClaudeClient | None = None, max_tagged: int = MAX_TAGGED_PER_CLIP,
                progress=None) -> list[Moment]:
    """Tag the top `max_tagged` moments (by motion) with Claude; the rest get heuristic tags."""
    client = client or get_client()
    ordered = sorted(moments, key=lambda m: m.motion_score, reverse=True)
    to_tag = ordered[:max_tagged]
    rest = ordered[max_tagged:]
    for m in rest:
        heuristic_tags(m)
    if not client.enabled:
        for m in to_tag:
            heuristic_tags(m)
        return moments
    for start in range(0, len(to_tag), MOMENTS_PER_CALL):
        batch = to_tag[start:start + MOMENTS_PER_CALL]
        content: list[dict] = []
        for i, m in enumerate(batch):
            content.append({"type": "text", "text": f"Moment {i}: {len(batch)} moments in this request; frames follow in time order."})
            for b64 in _moment_frames(proxy_path, m):
                content.append(image_block(b64))
        content.append({"type": "text", "text": f"Tag all {len(batch)} moments (indexes 0..{len(batch) - 1})."})
        try:
            result = client.structured(content, MomentTagBatch, budget=budget, system=VISION_SYSTEM, note="vision")
            by_index = {t.index: t for t in result.tags}
            for i, m in enumerate(batch):
                t = by_index.get(i)
                if t is None:
                    heuristic_tags(m)
                    continue
                apply_tag(m, t)
        except Exception as e:  # budget exhausted or API error: keep going with heuristics
            log.warning("vision tagging fell back to heuristics: %s", e)
            for m in batch:
                heuristic_tags(m)
            if "budget" in str(e).lower():
                for m in to_tag[start + MOMENTS_PER_CALL:]:
                    heuristic_tags(m)
                break
        if progress:
            progress(min(1.0, (start + len(batch)) / max(1, len(to_tag))))
    return moments


def apply_tag(m: Moment, t: MomentTag) -> None:
    m.species = t.species.strip().lower() or "animal"
    m.action = t.action if t.action in ACTIONS else "idle"
    m.intensity = int(t.intensity)
    m.framing = int(t.framing)
    m.subject_visible = bool(t.subject_visible)
    m.caption_hint = t.caption_hint.strip()
    m.habitat = t.habitat.strip().lower()
    m.lighting = t.lighting.strip().lower()
    m.dominant_color = t.dominant_color.strip().lower()
    m.outcome = t.outcome
    m.category = t.category
    m.score = score_moment(m)


def describe_clip(proxy_path: str | Path, duration: float, budget: BudgetTracker | None = None,
                  client: ClaudeClient | None = None) -> ClipDescription:
    """Short label for the Director chat ("gibbon swinging left to right, low angle")."""
    client = client or get_client()
    if not client.enabled:
        return ClipDescription(species="animal", description="clip", habitat="unknown", has_people=False, has_text=False)
    ts = [duration * f for f in (0.15, 0.5, 0.85)]
    frames = extract_frames(proxy_path, ts, width=FRAME_WIDTH)
    content: list[dict] = [image_block(frame_to_jpeg_b64(f, quality=70)) for f in frames]
    content.append({"type": "text", "text": "Describe this clip for an editor in under 12 words and name the main animal."})
    try:
        return client.structured(content, ClipDescription, budget=budget, system=VISION_SYSTEM, note="describe", max_tokens=400)
    except Exception as e:
        log.warning("describe_clip fell back: %s", e)
        return ClipDescription(species="animal", description="clip", habitat="unknown", has_people=False, has_text=False)

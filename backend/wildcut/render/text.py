"""Title rendering with Pillow: serif animal-name titles, letter-spacing, shadow, animations."""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from wildcut.config import get_settings

# logical font name -> (file, named instance for variable fonts)
FONT_ALIASES = {
    "cinzel-bold.ttf": ("Cinzel[wght].ttf", "Bold"),
    "cinzel-regular.ttf": ("Cinzel[wght].ttf", "Regular"),
    "cinzel-black.ttf": ("Cinzel[wght].ttf", "Black"),
    "cormorantgaramond-semibold.ttf": ("CormorantGaramond[wght].ttf", "SemiBold"),
    "cormorantgaramond-bold.ttf": ("CormorantGaramond[wght].ttf", "Bold"),
    "cormorantgaramond-regular.ttf": ("CormorantGaramond[wght].ttf", "Regular"),
    "anton-regular.ttf": ("Anton-Regular.ttf", None),
    "bebasneue-regular.ttf": ("BebasNeue-Regular.ttf", None),
}


def resolve_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    fonts_dir = get_settings().assets_dir / "fonts"
    key = name.lower()
    file, variation = FONT_ALIASES.get(key, (name, None))
    path = Path(file)
    if not path.is_absolute():
        path = fonts_dir / file
    if not path.exists():
        # fall back to any bundled serif, then to Pillow's default
        for cand in ("Cinzel[wght].ttf", "Anton-Regular.ttf"):
            if (fonts_dir / cand).exists():
                path = fonts_dir / cand
                variation = "Bold" if "Cinzel" in cand else None
                break
        else:
            return ImageFont.load_default(size)  # type: ignore[return-value]
    font = ImageFont.truetype(str(path), size)
    if variation:
        try:
            font.set_variation_by_name(variation)
        except Exception:
            pass
    return font


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


@lru_cache(maxsize=32)
def render_title_layer(text: str, font_name: str, size: int, letter_spacing: float, color: str, shadow: float,
                       stroke_px: int, max_width: int) -> Image.Image:
    """RGBA image of the title (tight bounds plus shadow margin). Cached per unique look."""
    font = resolve_font(font_name, size)
    spacing = int(round(letter_spacing * size))
    # shrink to fit max_width
    while size > 12:
        widths = [font.getlength(ch) for ch in text]
        total = sum(widths) + spacing * max(0, len(text) - 1)
        if total <= max_width:
            break
        size = int(size * 0.92)
        font = resolve_font(font_name, size)
        spacing = int(round(letter_spacing * size))
    widths = [font.getlength(ch) for ch in text]
    total = int(sum(widths) + spacing * max(0, len(text) - 1))
    asc, desc = font.getmetrics()
    margin = int(size * 0.35)
    W, H = total + 2 * margin, asc + desc + 2 * margin
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x = margin
    rgb = _hex(color)
    for ch, w in zip(text, widths):
        draw.text((x, margin), ch, font=font, fill=(*rgb, 255), stroke_width=stroke_px, stroke_fill=(0, 0, 0, 255))
        x += w + spacing
    if shadow > 0:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        x = margin
        for ch, w in zip(text, widths):
            sd.text((x, margin), ch, font=font, fill=(0, 0, 0, int(255 * min(1.0, shadow))), stroke_width=stroke_px, stroke_fill=(0, 0, 0, int(255 * min(1.0, shadow))))
            x += w + spacing
        sh = sh.filter(ImageFilter.GaussianBlur(size * 0.06))
        sh = sh.transform(sh.size, Image.AFFINE, (1, 0, -size * 0.02, 0, 1, -size * 0.03))
        layer = Image.alpha_composite(sh, layer)
    return layer


def _ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def title_state(item: dict, t: float, fps: int) -> tuple[float, float, float, float] | None:
    """(opacity, scale, dx, dy) for a text item at timeline t, or None if not visible."""
    start, dur = item["t"], item["duration"]
    k, ks = int(round(t * fps)), int(round(start * fps))
    if k < ks or k >= max(ks + 1, int(round((start + dur) * fps))):
        return None
    style = item.get("style", {})
    anim = item.get("animation", style.get("animation", "flash_in"))
    frame = k - ks
    p = frame / fps
    if anim == "fade":
        fade = float(style.get("fade", 1.0))
        fade = min(fade, dur / 2.5)
        if p < fade:
            op = p / fade
        elif p > dur - fade:
            op = max(0.0, (dur - p) / fade)
        else:
            op = 1.0
        return op, 1.0, 0.0, 0.0
    if anim == "slam":
        n = 4
        k = min(1.0, frame / n)
        scale = 1.4 + (1.0 - 1.4) * _ease_out(k)
        tail = max(2, int(0.12 * fps))
        op = 1.0 if p < dur - tail / fps else max(0.0, (dur - p) * fps / tail)
        dx, dy = _shake_offset(item["id"], frame, 0.012 if frame < 6 else 0.0)
        return op, scale, dx, dy
    # flash_in (default): scale snaps 1.12 -> 1.0 in 3 frames, short shake, hard hold, 3-frame out
    n = 3
    k = min(1.0, frame / n)
    scale = 1.12 + (1.0 - 1.12) * _ease_out(k)
    tail = 3
    op = 1.0 if p < dur - tail / fps else max(0.0, (dur - p) * fps / tail)
    amp = 0.008 if (frame < 8 and style.get("shake", True)) else 0.0
    dx, dy = _shake_offset(item["id"], frame, amp)
    return op, scale, dx, dy


def _shake_offset(seed_key: str, frame: int, amp: float) -> tuple[float, float]:
    if amp <= 0:
        return 0.0, 0.0
    rng = np.random.default_rng(abs(hash((seed_key, frame))) % (2 ** 32))
    decay = max(0.0, 1 - frame / 8)
    return float(rng.uniform(-1, 1) * amp * decay), float(rng.uniform(-1, 1) * amp * decay)


def composite_title(frame_bgr: np.ndarray, item: dict, t: float, fps: int) -> np.ndarray:
    state = title_state(item, t, fps)
    if state is None:
        return frame_bgr
    opacity, scale, dx, dy = state
    if opacity <= 0.001:
        return frame_bgr
    H, W = frame_bgr.shape[:2]
    style = item.get("style", {})
    size = int(round(float(style.get("size_frac", 0.075)) * W))
    layer = render_title_layer(item["text"], style.get("font", "Cinzel-Bold.ttf"), size, float(style.get("letter_spacing", 0.15)),
                               style.get("color", "#FFFFFF"), float(style.get("shadow", 0.5)), int(style.get("stroke_px", 0)),
                               int(W * 0.9))
    if abs(scale - 1.0) > 1e-3:
        layer = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))), Image.LANCZOS)
    pos = style.get("position", [0.5, 0.5])
    cx, cy = pos[0] * W + dx * W, pos[1] * H + dy * H
    x0, y0 = int(round(cx - layer.width / 2)), int(round(cy - layer.height / 2))
    # paste with clipping
    lx0, ly0 = max(0, -x0), max(0, -y0)
    lx1, ly1 = min(layer.width, W - x0), min(layer.height, H - y0)
    if lx1 <= lx0 or ly1 <= ly0:
        return frame_bgr
    sub = np.asarray(layer)[ly0:ly1, lx0:lx1]
    alpha = (sub[..., 3:4].astype(np.float32) / 255.0) * opacity
    rgb = sub[..., :3][..., ::-1].astype(np.float32)
    region = frame_bgr[y0 + ly0:y0 + ly1, x0 + lx0:x0 + lx1].astype(np.float32)
    blended = region * (1 - alpha) + rgb * alpha
    out = frame_bgr.copy()
    out[y0 + ly0:y0 + ly1, x0 + lx0:x0 + lx1] = np.clip(blended + 0.5, 0, 255).astype(np.uint8)
    return out


def draw_text_block(img: Image.Image, text: str, font_name: str, size: int, xy: tuple[int, int], color: str = "#FFFFFF",
                    anchor: str = "mm", letter_spacing: float = 0.0, max_width: int | None = None) -> None:
    """Simple text helper for Showdown cards (Pillow anchor semantics)."""
    font = resolve_font(font_name, size)
    if max_width:
        while size > 10 and font.getlength(text) + letter_spacing * size * max(0, len(text) - 1) > max_width:
            size = int(size * 0.92)
            font = resolve_font(font_name, size)
    draw = ImageDraw.Draw(img)
    if letter_spacing <= 0:
        draw.text(xy, text, font=font, fill=color, anchor=anchor)
        return
    spacing = letter_spacing * size
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + spacing * max(0, len(text) - 1)
    x = xy[0] - total / 2 if anchor[0] == "m" else (xy[0] - total if anchor[0] == "r" else xy[0])
    for ch, w in zip(text, widths):
        draw.text((x, xy[1]), ch, font=font, fill=color, anchor="l" + anchor[1])
        x += w + spacing

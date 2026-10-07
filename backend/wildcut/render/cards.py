"""Showdown card renderer: draws `kind: "card"` EDL clips from a layout JSON.

Everything (positions, fonts, colors, animation curves) comes from the layout file so new
layouts need no code changes. Media frames are graded with the EDL grade (black-and-white LUT);
chrome (cards, text, stamps) is drawn after grading so it stays crisp.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from wildcut.render.grade import Grade
from wildcut.render.text import resolve_font


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _ease_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


class LoopSource:
    """Sequential decoder that loops a short clip (animal media on a card)."""

    def __init__(self, path: str):
        import av

        self.path = path
        self.container = av.open(path)
        self.stream = self.container.streams.video[0]
        self.tb = float(self.stream.time_base)
        self.duration = float(self.stream.duration * self.tb) if self.stream.duration else 3.0
        self._iter = self.container.decode(self.stream)
        self.cur = None   # (t, array)
        self.nxt = None

    def _decode(self):
        for f in self._iter:
            if f.pts is None:
                continue
            return float(f.pts * self.tb), f
        return None

    def _restart(self) -> None:
        self.container.seek(0)
        self._iter = self.container.decode(self.stream)
        self.cur = None
        self.nxt = None

    def frame_at(self, elapsed: float) -> np.ndarray:
        t = elapsed % max(0.2, self.duration)
        if self.cur is not None and t < self.cur[0] - 1e-3:
            self._restart()
        if self.nxt is None:
            got = self._decode()
            if got is None:
                self._restart()
                got = self._decode()
            self.nxt = got
        while self.nxt is not None and self.nxt[0] <= t + 1e-6:
            self.cur = (self.nxt[0], self.nxt[1].to_ndarray(format="bgr24"))
            self.nxt = self._decode()
        if self.cur is None:
            if self.nxt is None:
                return np.zeros((16, 16, 3), np.uint8)
            self.cur = (self.nxt[0], self.nxt[1].to_ndarray(format="bgr24"))
        return self.cur[1]

    def close(self) -> None:
        try:
            self.container.close()
        except Exception:
            pass


def _cover(frame_bgr: np.ndarray, w: int, h: int) -> np.ndarray:
    import cv2

    sh, sw = frame_bgr.shape[:2]
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))
    r = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x0, y0 = (nw - w) // 2, (nh - h) // 2
    return r[y0:y0 + h, x0:x0 + w]


class CardRenderer:
    def __init__(self, edl: dict, layout: dict, width: int, height: int):
        self.edl = edl
        self.layout = layout
        self.w, self.h = width, height
        self.grade = Grade(edl.get("grade", {}))
        self.sources: dict[int, LoopSource | None] = {}
        self.sd = edl["showdown"]
        self.fps = int(edl.get("fps", 30))
        from wildcut.planner.presets import load_preset

        self.cfg = load_preset("showdown")["showdown"]

    # ---- media
    def media_frame(self, idx: int, elapsed: float, w: int, h: int) -> np.ndarray:
        animal = self.sd["animals"][idx]
        path = animal.get("proxy") or animal.get("src")
        if idx not in self.sources:
            self.sources[idx] = LoopSource(path) if path and Path(path).exists() else None
        src = self.sources[idx]
        if src is None:
            # placeholder: dark panel with the animal's initial
            img = Image.new("RGB", (w, h), (34, 34, 38))
            d = ImageDraw.Draw(img)
            f = resolve_font("Anton-Regular.ttf", int(h * 0.4))
            d.text((w / 2, h / 2), animal["name"][:1].upper(), font=f, fill=(90, 90, 96), anchor="mm")
            return np.asarray(img)[..., ::-1].copy()
        frame = _cover(src.frame_at(elapsed), w, h)
        return self.grade.apply(frame)

    # ---- drawing helpers
    def _card_rect(self, slot: str, single: bool) -> tuple[int, int, int, int]:
        L = self.layout
        pad = L["canvas"]["padding"] * self.w
        gap = L["canvas"]["gap"] * self.w
        top = L["card"]["top"] * self.h
        bottom = L["card"]["bottom"] * self.h
        if single:
            cw = L["winner"]["card_width"] * self.w
            x0 = (self.w - cw) / 2
            return int(x0), int(L["winner"]["top"] * self.h), int(x0 + cw), int(L["winner"]["bottom"] * self.h)
        cw = (self.w - 2 * pad - gap) / 2
        x0 = pad if slot == "left" else pad + cw + gap
        return int(x0), int(top), int(x0 + cw), int(bottom)

    def draw_card(self, img: Image.Image, idx: int, rect: tuple[int, int, int, int], elapsed: float, seg_len: float,
                  counter_p: float, dx: int = 0) -> None:
        L = self.layout["card"]
        x0, y0, x1, y1 = rect
        x0 += dx
        x1 += dx
        cw, ch = x1 - x0, y1 - y0
        d = ImageDraw.Draw(img)
        r = int(L["radius"] * self.w)
        d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=_hex(L["background"]), outline=_hex(L["border"]), width=int(L["border_px"]))
        animal = self.sd["animals"][idx]
        # name
        nf = L["name"]
        font = resolve_font(nf["font"], int(nf["size_frac"] * self.w))
        name = animal["name"].upper()
        while font.getlength(name) > cw * 0.9 and font.size > 10:
            font = resolve_font(nf["font"], int(font.size * 0.9))
        d.text((x0 + cw / 2, y0 + nf["y"] * ch), name, font=font, fill=_hex(nf["color"]), anchor="mm")
        # pill with counter
        pf = L["pill"]
        ph = pf["height"] * ch
        pw = pf["width"] * cw
        px0, py0 = x0 + (cw - pw) / 2, y0 + pf["y"] * ch - ph / 2
        d.rounded_rectangle((px0, py0, px0 + pw, py0 + ph), radius=int(ph / 2), fill=_hex(pf["background"]))
        value = animal["value"] * _ease_out(counter_p) if animal.get("value") is not None else None
        from wildcut.planner.showdown import _fmt_value

        vfont = resolve_font(pf["font"], int(pf["size_frac"] * self.w))
        ufont = resolve_font(pf["font"], int(pf["unit_size_frac"] * self.w))
        vtext = _fmt_value(value)
        unit = animal.get("unit", "")
        vw = vfont.getlength(vtext)
        uw = ufont.getlength(" " + unit) if unit else 0
        sx = px0 + pw / 2 - (vw + uw) / 2
        d.text((sx, py0 + ph / 2), vtext, font=vfont, fill=_hex(pf["color"]), anchor="lm")
        if unit:
            d.text((sx + vw, py0 + ph / 2 + ph * 0.08), " " + unit, font=ufont, fill=_hex(pf["color"]), anchor="lm")
        # media
        mf = L["media"]
        inset = mf["inset"] * cw
        mx0, my0 = int(x0 + inset), int(y0 + mf["top"] * ch)
        mx1, my1 = int(x1 - inset), int(y0 + mf["bottom"] * ch)
        if mx1 - mx0 > 4 and my1 - my0 > 4:
            frame = self.media_frame(idx, elapsed, mx1 - mx0, my1 - my0)
            tile = Image.fromarray(frame[..., ::-1])
            mask = Image.new("L", tile.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width - 1, tile.height - 1), radius=int(mf["radius"] * self.w), fill=255)
            img.paste(tile, (mx0, my0), mask)
        # fact
        ff = L["fact"]
        fact = animal.get("fact", "")
        if fact:
            ffont = resolve_font(ff["font"], int(ff["size_frac"] * self.w))
            lines = _wrap(fact, ffont, cw * 0.9, ff.get("max_lines", 2))
            lh = ffont.size * 1.25
            y = y0 + ff["y"] * ch - lh * (len(lines) - 1) / 2
            for ln in lines:
                d.text((x0 + cw / 2, y), ln, font=ffont, fill=_hex(ff["color"]), anchor="mm")
                y += lh

    def draw_stamp(self, img: Image.Image, rect: tuple[int, int, int, int], text: str, scale: float, dx: int = 0) -> None:
        S = self.layout["stamp"]
        font = resolve_font(S["font"], int(S["size_frac"] * self.w * scale))
        tw, th = font.getlength(text), font.size * 1.2
        pad = int(S["stroke_px"] * 2)
        layer = Image.new("RGBA", (int(tw) + 4 * pad, int(th) + 4 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((layer.width / 2, layer.height / 2), text, font=font, fill=_hex(S["color"]) + (255,), anchor="mm",
                                   stroke_width=int(S["stroke_px"] * scale), stroke_fill=_hex(S["stroke"]) + (255,))
        layer = layer.rotate(S["angle_deg"], resample=Image.BICUBIC, expand=True)
        x0, y0, x1, y1 = rect
        cx, cy = (x0 + x1) / 2 + dx, y0 + S["y"] * (y1 - y0)
        img.alpha_composite(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)))

    def draw_header(self, img: Image.Image, text: str, hcfg: dict) -> None:
        font = resolve_font(hcfg["font"], int(hcfg["size_frac"] * self.w))
        while font.getlength(text) > self.w * 0.92 and font.size > 12:
            font = resolve_font(hcfg["font"], int(font.size * 0.9))
        ImageDraw.Draw(img).text((self.w / 2, hcfg["y"] * self.h), text, font=font, fill=_hex(hcfg["color"]), anchor="mm")

    # ---- entry point used by the composer
    def __call__(self, edl: dict, clip: dict, t: float, k: int, out_w: int, out_h: int) -> np.ndarray:
        L = self.layout
        card = clip["card"]
        seg = clip["tl_duration"]
        p_seg = (t - clip["start"]) / max(seg, 1e-6)
        elapsed = t  # media loops on absolute time so cuts between cards don't restart the loop
        img = Image.new("RGBA", (self.w, self.h), _hex(L["canvas"]["background"]) + (255,))
        ctype = card["type"]
        slide_in = self.cfg.get("slide_in", 0.25)
        slide_out = self.cfg.get("slide_out", 0.2)
        counter_frac = self.cfg.get("counter_fraction", 0.6)
        stamp_at = self.cfg.get("stamp_at", 0.8)
        if ctype == "winner":
            self.draw_header(img, L["winner"]["header"]["text"].format(winner_header=self.sd["winner_header"], stat_label=self.sd["label"]), L["winner"]["header"])
            rect = self._card_rect("left", single=True)
            # expand in over the first 0.3 s
            grow = _ease_out((t - clip["start"]) / 0.3)
            x0, y0, x1, y1 = rect
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            s = 0.8 + 0.2 * grow
            rect = (int(cx - (cx - x0) * s), int(cy - (cy - y0) * s), int(cx + (x1 - cx) * s), int(cy + (y1 - cy) * s))
            self.draw_card(img, card["left"], rect, elapsed, seg, 1.0)
            return np.asarray(img.convert("RGB"))[..., ::-1].copy()
        self.draw_header(img, L["header"]["text"].format(stat_label=self.sd["label"], winner_header=self.sd["winner_header"]), L["header"])
        left_rect = self._card_rect("left", single=False)
        counter_p = min(1.0, (t - clip["start"]) / max(1e-6, counter_frac * seg))
        if ctype == "intro":
            self.draw_card(img, card["left"], left_rect, elapsed, seg, counter_p)
            return np.asarray(img.convert("RGB"))[..., ::-1].copy()
        right_rect = self._card_rect("right", single=False)
        loser = card.get("loser")
        # slide-in of the challenger from the right
        tin = (t - clip["start"]) / max(slide_in, 1e-6)
        dx_in = int((1 - _ease_out(tin)) * (self.w - right_rect[0])) if tin < 1 else 0
        # slide-out of the loser during the last `slide_out` seconds
        tout = (t - (clip["start"] + seg - slide_out)) / max(slide_out, 1e-6)
        out_px = int(_ease_out(tout) * self.w) if tout > 0 else 0
        left_dx = -out_px if loser == "left" else 0
        right_dx = dx_in + (out_px if loser == "right" else 0)
        self.draw_card(img, card["left"], left_rect, elapsed, seg, 1.0, dx=left_dx)
        self.draw_card(img, card["right"], right_rect, elapsed, seg, counter_p, dx=right_dx)
        if loser and p_seg >= stamp_at and tout <= 0.5:
            frames_in = (t - (clip["start"] + stamp_at * seg)) * self.fps
            n = self.layout["animation"].get("stamp_frames", 4)
            k0 = min(1.0, frames_in / max(1, n))
            scale = self.layout["animation"].get("stamp_scale_from", 1.6) + (1.0 - self.layout["animation"].get("stamp_scale_from", 1.6)) * _ease_out(k0)
            self.draw_stamp(img, left_rect if loser == "left" else right_rect, card.get("stamp", "LOSER"), scale,
                            dx=left_dx if loser == "left" else right_dx)
        return np.asarray(img.convert("RGB"))[..., ::-1].copy()

    def close(self) -> None:
        for s in self.sources.values():
            if s:
                s.close()


def _wrap(text: str, font, max_w: float, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and font.getlength(lines[-1]) > max_w:
        while font.getlength(lines[-1] + "…") > max_w and len(lines[-1]) > 2:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def make_card_renderer(edl: dict, width: int, height: int) -> CardRenderer:
    from wildcut.planner.showdown import load_layout

    return CardRenderer(edl, load_layout(edl.get("showdown", {}).get("layout", "default")), width, height)

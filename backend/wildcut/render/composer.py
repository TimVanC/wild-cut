"""Frame composer: EDL -> MP4. Deterministic: same EDL, same settings, same bytes.

Per output frame: find the clip, map timeline -> source time through the speed curve, fetch the
frame, apply the crop path + headroom zoom + effect geometry (zoom punch, push-in, shake with
rotation) in a single affine resample, then grade, chromatic/glitch/flash/fade, title, overlays.
"""
from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from wildcut.analysis.tracking import CropPath
from wildcut.media import MediaError
from wildcut.planner import edl as edlmod
from wildcut.planner.speed import _rate_at, source_at
from wildcut.render import effects as fx
from wildcut.render.grade import Grade
from wildcut.render.text import composite_title

HEADROOM = {"phonk": 1.06, "chase": 1.06, "cinematic": 1.0, "showdown": 1.0}


@dataclass
class RenderSettings:
    quality: str = "preview"          # preview (540 wide) | full (1080 wide)
    width: int | None = None
    use_proxy: bool | None = None     # default: proxy for preview, source for full
    smooth_slowmo: bool | None = None # minterpolate for ramped clips (default: full only)
    crf: int | None = None
    preset: str | None = None
    with_audio: bool = True
    fps: int | None = None

    def resolve(self, edl: dict) -> RenderSettings:
        s = RenderSettings(**self.__dict__)
        if s.width is None:
            s.width = edlmod.RENDER_WIDTHS.get(s.quality, 540)
        if s.use_proxy is None:
            s.use_proxy = s.quality == "preview"
        if s.smooth_slowmo is None:
            s.smooth_slowmo = s.quality == "full"
        if s.crf is None:
            s.crf = 23 if s.quality == "preview" else 18
        if s.preset is None:
            s.preset = "veryfast" if s.quality == "preview" else "medium"
        if s.fps is None:
            s.fps = int(edl.get("fps", 30))
        return s


class Encoder:
    def __init__(self, path: Path, width: int, height: int, fps: int, crf: int, preset: str):
        self.path = path
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-x264-params", "threads=4:sliced-threads=1", "-g", str(fps),
            "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
            str(path),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame.tobytes())

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
        self.proc.wait()
        if self.proc.returncode != 0:
            raise MediaError(f"encode failed: {err[-800:]}")


def frame_box(frame: float | str | None, out_w: int, out_h: int) -> tuple[int, int]:
    """Pixel size of the picture box for a clip's `frame` aspect inside the canvas (even sizes).
    None/0/"fill" fills the canvas; otherwise the box has the given w/h aspect and touches the canvas
    on its long side, centered, with black elsewhere (Tim's "1.2:1 in the middle with bars")."""
    fa = parse_frame(frame)
    if fa is None:
        return out_w, out_h
    if fa >= out_w / out_h:
        w, h = out_w, int(round(out_w / fa / 2)) * 2
    else:
        w, h = int(round(out_h * fa / 2)) * 2, out_h
    return max(2, min(w, out_w)), max(2, min(h, out_h))


def parse_frame(frame: float | str | None) -> float | None:
    """1.2 | "1.2" | "1.2:1" | "16:9" -> aspect; None / 0 / "fill" -> None."""
    if frame in (None, 0, "", "fill", "none"):
        return None
    if isinstance(frame, str):
        if ":" in frame:
            a, b = frame.split(":", 1)
            return float(a) / float(b)
        return float(frame)
    return float(frame) if float(frame) > 0 else None


class ClipRenderer:
    """Holds the decoder and geometry for one EDL clip."""

    def __init__(self, clip: dict, settings: RenderSettings, out_w: int, out_h: int, headroom: float, tmp: Path):
        from wildcut.render.frames import ClipFrameSource, prepare_smooth

        self.clip = clip
        self.out_w, self.out_h = out_w, out_h
        self.headroom = headroom
        path = clip["proxy"] if (settings.use_proxy and clip.get("proxy")) else clip["src"]
        if not Path(path).exists():
            path = clip["src"]
        # documentary sources are letterboxed: the proxy is already cropped, the source is not
        self.src_crop = clip.get("src_crop") if path == clip["src"] and clip.get("src_crop") else None
        self.speed = clip.get("speed")
        min_rate = min((k["rate"] for k in self.speed), default=1.0) if self.speed else 1.0
        offset = 0.0
        if settings.smooth_slowmo and self.speed and min_rate < 0.75:
            target = settings.fps / max(0.2, min_rate)
            smooth = tmp / f"smooth_{clip['id']}.mp4"
            try:
                prepare_smooth(path, clip["in"], clip["out"], smooth, target)
                offset = max(0.0, clip["in"] - 0.1)
                path = str(smooth)
            except MediaError:
                offset = 0.0
        self.source = ClipFrameSource(path, clip["in"], clip["out"], blend=True, time_offset=offset)
        self.src_w, self.src_h = self.source.width, self.source.height
        self.crop = CropPath.from_dict(clip["crop_path"]) if clip.get("crop_path") else None
        # "frame": show the clip as a centered box of this aspect (e.g. 1.2) with black around it,
        # instead of cropping it to fill the whole canvas. None/0 = fill.
        self.box_w, self.box_h = frame_box(clip.get("frame"), out_w, out_h)

    def frame(self, t: float, frame_idx: int, geom: fx.GeomState) -> np.ndarray:
        c = self.clip
        s = source_at(self.speed, c["in"], c["out"], t - c["start"])
        rate = _rate_at(self.speed, s) if self.speed else 1.0
        src = self.source.frame_at(s, rate)
        if self.src_crop:
            x, y, w, h = [int(v) for v in self.src_crop]
            src = src[y:y + h, x:x + w]
        if src.shape[1] != self.src_w or src.shape[0] != self.src_h:
            self.src_w, self.src_h = src.shape[1], src.shape[0]
        # crop window in source pixels
        if self.crop is not None:
            cx, cy = self.crop.center_at(s)
            cw, ch = self.crop.cw, self.crop.ch
        else:
            cx, cy = 0.5, 0.5
            cw, ch = 1.0, 1.0
        # fix aspect of the crop to the box aspect (crop paths were built for the canvas; a framed clip
        # keeps the path's center and widens the crop to the box)
        box_w, box_h = self.box_w, self.box_h
        src_aspect = self.src_w / self.src_h
        box_aspect = box_w / box_h
        if abs((cw * src_aspect) / ch - box_aspect) > 1e-3:
            if box_aspect < src_aspect:
                ch, cw = 1.0, box_aspect / src_aspect
            else:
                cw, ch = 1.0, src_aspect / box_aspect
        zoom = self.headroom * geom.zoom
        cw_px = cw * self.src_w / zoom
        ch_px = ch * self.src_h / zoom
        cx_px = cx * self.src_w + geom.dx * cw_px
        cy_px = cy * self.src_h + geom.dy * ch_px
        # clamp so the (unrotated) crop stays inside the frame: shake never shows black edges
        half_w, half_h = cw_px / 2, ch_px / 2
        cx_px = min(max(cx_px, half_w), self.src_w - half_w)
        cy_px = min(max(cy_px, half_h), self.src_h - half_h)
        scale = box_w / cw_px
        M = cv2.getRotationMatrix2D((cx_px, cy_px), geom.rot, scale)
        M[0, 2] += box_w / 2 - cx_px
        M[1, 2] += box_h / 2 - cy_px
        box = cv2.warpAffine(src, M, (box_w, box_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
        if (box_w, box_h) == (self.out_w, self.out_h):
            return box
        out = np.zeros((self.out_h, self.out_w, 3), np.uint8)
        x0, y0 = (self.out_w - box_w) // 2, (self.out_h - box_h) // 2
        out[y0:y0 + box_h, x0:x0 + box_w] = box
        return out

    def close(self) -> None:
        self.source.close()


def render_video(edl: dict, out_path: str | Path, settings: RenderSettings | None = None,
                 segment: tuple[float, float] | None = None, progress: Callable[[float], None] | None = None,
                 card_renderer: Callable | None = None) -> Path:
    """Render the EDL (or a timeline segment) to a video-only MP4."""
    settings = (settings or RenderSettings()).resolve(edl)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fps = settings.fps
    out_w, out_h = edlmod.output_size(edl["aspect"], settings.width)
    headroom = HEADROOM.get(edl.get("style", "phonk"), 1.04)
    grade = Grade(edl.get("grade", {}))
    overlays = fx.Overlays(edl.get("overlays", {}), out_w, out_h)
    effects = [e for e in edl.get("effects", []) if e.get("enabled", True)]
    texts = [t for t in edl.get("text", []) if t.get("enabled", True)]
    trans = edl.get("transitions", {})
    duration = float(edl["duration"])
    t_start, t_end = (0.0, duration) if segment is None else (max(0.0, segment[0]), min(duration, segment[1]))
    first = int(math.ceil(t_start * fps - 1e-6))
    last = int(math.ceil(t_end * fps - 1e-6))
    if last <= first:
        last = first + 1
    tmp = Path(tempfile.mkdtemp(prefix="wildcut_render_"))
    if card_renderer is None and edl.get("showdown"):
        from wildcut.render.cards import make_card_renderer

        card_renderer = make_card_renderer(edl, out_w, out_h)
    enc = Encoder(out_path, out_w, out_h, fps, settings.crf, settings.preset)
    renderers: dict[str, ClipRenderer] = {}
    current_id = None
    black = np.zeros((out_h, out_w, 3), np.uint8)
    fade_in, fade_out = float(trans.get("fade_in", 0.0)), float(trans.get("fade_out", 0.0))
    try:
        for k in range(first, last):
            t = k / fps
            clip = edlmod.clip_at(edl, t)
            act = fx.active(effects, t, fps)
            geom = fx.geometry(act, t, k, fps)
            if clip is None:
                frame = black.copy()
            elif clip.get("kind") == "card" and card_renderer is not None:
                frame = card_renderer(edl, clip, t, k, out_w, out_h)
            else:
                if clip["id"] != current_id:
                    if current_id in renderers:
                        renderers.pop(current_id).close()
                    if progress and clip.get("speed") and settings.smooth_slowmo:
                        progress((k - first) / max(1, last - first))
                    renderers[clip["id"]] = ClipRenderer(clip, settings, out_w, out_h, headroom, tmp)
                    current_id = clip["id"]
                frame = renderers[clip["id"]].frame(t, k, geom)
                frame = grade.apply(frame)
            frame = fx.apply_motion_blur(frame, fx.motion_blur_strength(act, t))
            px = fx.chromatic_px(act, t, out_w)
            if px:
                frame = fx.apply_chromatic(frame, px)
            frame = fx.apply_glitch(frame, act, t, k)
            frame = overlays.apply(frame, k)
            dim = fx.fade_black_opacity(act, t)
            if fade_in > 0 and t < fade_in:
                dim = max(dim, 1 - t / fade_in)
            if fade_out > 0 and t > duration - fade_out:
                dim = max(dim, (t - (duration - fade_out)) / fade_out)
            frame = fx.apply_dim(frame, dim)
            frame = fx.apply_flash(frame, fx.flash_opacity(act, t))
            for item in texts:
                frame = composite_title(frame, item, t, fps)
            enc.write(np.ascontiguousarray(frame))
            if progress and (k - first) % 15 == 0:
                progress((k - first + 1) / (last - first))
    finally:
        for r in renderers.values():
            r.close()
        if card_renderer is not None and hasattr(card_renderer, "close"):
            card_renderer.close()
        enc.close()
    if progress:
        progress(1.0)
    return out_path


def render(edl: dict, out_path: str | Path, settings: RenderSettings | None = None,
           progress: Callable[[float], None] | None = None, card_renderer: Callable | None = None) -> Path:
    """Full render with audio per edl.audio.export. Returns the final MP4 path."""
    from wildcut.render.audio import build_original_audio, build_song_audio, mux

    settings = (settings or RenderSettings()).resolve(edl)
    out_path = Path(out_path)
    tmp = Path(tempfile.mkdtemp(prefix="wildcut_out_"))
    video = render_video(edl, tmp / "video.mp4", settings, progress=progress, card_renderer=card_renderer)
    audio = None
    mode = edl.get("audio", {}).get("export", "silent")
    if settings.with_audio and mode == "mixed" and edl["audio"].get("song_path") and edl["audio"].get("song_window"):
        audio = build_song_audio(edl["audio"]["song_path"], edl["audio"]["song_window"], float(edl["duration"]), tmp / "song.m4a")
    elif settings.with_audio and mode == "original":
        audio = build_original_audio(edl, tmp / "orig.m4a")
    return mux(video, audio, out_path)


def render_frame(edl: dict, t: float, width: int = 540, card_renderer: Callable | None = None) -> np.ndarray:
    """Single composed frame at timeline t (BGR), for thumbnails and tuning."""
    settings = RenderSettings(quality="preview", width=width, smooth_slowmo=False).resolve(edl)
    fps = settings.fps
    k = int(round(t * fps))
    t = k / fps
    out_w, out_h = edlmod.output_size(edl["aspect"], width)
    headroom = HEADROOM.get(edl.get("style", "phonk"), 1.04)
    grade = Grade(edl.get("grade", {}))
    overlays = fx.Overlays(edl.get("overlays", {}), out_w, out_h)
    effects = [e for e in edl.get("effects", []) if e.get("enabled", True)]
    act = fx.active(effects, t, fps)
    geom = fx.geometry(act, t, k, fps)
    clip = edlmod.clip_at(edl, t)
    if card_renderer is None and edl.get("showdown"):
        from wildcut.render.cards import make_card_renderer

        card_renderer = make_card_renderer(edl, out_w, out_h)
    if clip is None:
        frame = np.zeros((out_h, out_w, 3), np.uint8)
    elif clip.get("kind") == "card" and card_renderer is not None:
        frame = card_renderer(edl, clip, t, k, out_w, out_h)
    else:
        r = ClipRenderer(clip, settings, out_w, out_h, headroom, Path(tempfile.mkdtemp(prefix="wildcut_frame_")))
        try:
            frame = grade.apply(r.frame(t, k, geom))
        finally:
            r.close()
    frame = fx.apply_motion_blur(frame, fx.motion_blur_strength(act, t))
    px = fx.chromatic_px(act, t, out_w)
    if px:
        frame = fx.apply_chromatic(frame, px)
    frame = fx.apply_glitch(frame, act, t, k)
    frame = overlays.apply(frame, k)
    frame = fx.apply_dim(frame, fx.fade_black_opacity(act, t))
    frame = fx.apply_flash(frame, fx.flash_opacity(act, t))
    for item in edl.get("text", []):
        if item.get("enabled", True):
            frame = composite_title(frame, item, t, fps)
    return frame

"""ffmpeg/ffprobe/PyAV helpers: probing, proxies, frame extraction, thumbnails."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from fractions import Fraction
from pathlib import Path

import numpy as np

PROXY_HEIGHT = 540
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


class MediaError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@dataclass
class MediaInfo:
    path: str
    duration: float
    fps: float
    width: int
    height: int
    has_audio: bool
    has_video: bool
    codec: str = ""
    nb_frames: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise MediaError(f"file not found: {path}")
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise MediaError(f"ffprobe failed for {path}: {e.stderr.strip()}") from e
    data = json.loads(out)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0.0)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = 0.0
    width = height = 0
    codec = ""
    nb_frames = 0
    if v:
        rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        try:
            fps = float(Fraction(rate)) if "/" in rate else float(rate)
        except (ZeroDivisionError, ValueError):
            fps = 0.0
        width = int(v.get("width") or 0)
        height = int(v.get("height") or 0)
        # respect rotation metadata so portrait phone clips report portrait dimensions
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if (v.get("tags") or {}).get("rotate"):
            rot = int(v["tags"]["rotate"])
        if rot % 180 != 0:
            width, height = height, width
        codec = v.get("codec_name", "")
        nb_frames = int(v.get("nb_frames") or 0)
        if not duration and v.get("duration"):
            duration = float(v["duration"])
    if a and not duration and a.get("duration"):
        duration = float(a["duration"])
    return MediaInfo(str(path), duration, fps, width, height, a is not None, v is not None, codec, nb_frames)


def make_proxy(src: str | Path, dst: str | Path, height: int = PROXY_HEIGHT) -> Path:
    """540p H.264 proxy with a short GOP for fast seeking; audio kept (AAC) when present."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-vf", f"scale=-2:{height}:flags=bicubic",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-g", "15",
        "-movflags", "+faststart",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd.append(str(dst))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"proxy failed for {src}: {r.stderr.strip()[-800:]}")
    return dst


def image_to_video(src: str | Path, dst: str | Path, duration: float = 6.0, height: int = 1080) -> Path:
    """Turn a still (Showdown media, stock photo) into a short H.264 clip."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-i", str(src),
        "-t", f"{duration:.3f}", "-vf", f"scale=-2:{height}:flags=bicubic,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"image_to_video failed: {r.stderr.strip()[-800:]}")
    return dst


def iter_frames(path: str | Path, sample_fps: float | None = None, width: int | None = None,
                start: float = 0.0, end: float | None = None, gray: bool = False):
    """Yield (time_seconds, ndarray) frames. With sample_fps, frames are decimated to that rate."""
    import av

    container = av.open(str(path))
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    tb = float(stream.time_base) if stream.time_base else None
    if start > 0:
        try:
            container.seek(int(start / tb) if tb else int(start * 1e6), stream=stream if tb else None, backward=True, any_frame=False)
        except Exception:
            pass
    next_sample = start
    step = (1.0 / sample_fps) if sample_fps else 0.0
    fmt = "gray" if gray else "bgr24"
    try:
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            t = float(frame.pts * stream.time_base)
            if t < start - 1e-6:
                continue
            if end is not None and t > end + 1e-6:
                break
            if sample_fps and t + 1e-6 < next_sample:
                continue
            if width and frame.width != width:
                h = int(round(frame.height * width / frame.width / 2)) * 2
                frame = frame.reformat(width=width, height=h, format=fmt)
                arr = frame.to_ndarray()
            else:
                arr = frame.to_ndarray(format=fmt)
            yield t, arr
            if sample_fps:
                # advance to the next sample slot, skipping any we fell behind on
                while next_sample <= t + 1e-6:
                    next_sample += step
    finally:
        container.close()


def extract_frames(path: str | Path, times: list[float], width: int | None = None) -> list[np.ndarray]:
    """Frames nearest to each requested time (BGR). Times are processed in sorted order."""
    import av

    out: dict[int, np.ndarray] = {}
    order = sorted(range(len(times)), key=lambda i: times[i])
    container = av.open(str(path))
    stream = container.streams.video[0]
    try:
        for idx in order:
            target = times[idx]
            tb = stream.time_base
            try:
                container.seek(int(max(0.0, target - 0.5) / tb), stream=stream, backward=True, any_frame=False)
            except Exception:
                container.seek(0)
            best = None
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                t = float(frame.pts * tb)
                if best is None or abs(t - target) < abs(best[0] - target):
                    best = (t, frame)
                if t >= target:
                    break
            if best is None:
                continue
            frame = best[1]
            if width and frame.width != width:
                h = int(round(frame.height * width / frame.width / 2)) * 2
                frame = frame.reformat(width=width, height=h, format="bgr24")
                out[idx] = frame.to_ndarray()
            else:
                out[idx] = frame.to_ndarray(format="bgr24")
    finally:
        container.close()
    return [out[i] for i in range(len(times)) if i in out]


def thumbnail(path: str | Path, t: float, dst: str | Path, width: int = 320) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={width}:-2", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"thumbnail failed: {r.stderr.strip()[-400:]}")
    return dst


def frame_to_jpeg_b64(frame_bgr: np.ndarray, quality: int = 80, max_width: int = 768) -> str:
    import cv2

    if frame_bgr.shape[1] > max_width:
        h = int(round(frame_bgr.shape[0] * max_width / frame_bgr.shape[1]))
        frame_bgr = cv2.resize(frame_bgr, (max_width, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise MediaError("jpeg encode failed")
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")


def extract_audio_wav(src: str | Path, dst: str | Path, sr: int = 22050, start: float | None = None,
                      end: float | None = None) -> Path:
    """Mono WAV for analysis (librosa) or for the Showdown/visual-mode original-audio mix."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if end is not None and start is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"audio extract failed: {r.stderr.strip()[-400:]}")
    return dst

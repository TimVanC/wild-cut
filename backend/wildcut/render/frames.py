"""Sequential frame access for one EDL clip, with frame blending for slow-mo.

Timeline frames walk through a clip with monotonic source time, so a forward decoder with a
two-frame window is enough. For smooth slow-mo on full exports, `prepare_smooth` renders the
ramped range through ffmpeg's minterpolate at a higher frame rate and the source then reads
from that intermediate file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from wildcut.media import MediaError


class ClipFrameSource:
    def __init__(self, path: str | Path, in_t: float, out_t: float, blend: bool = True, time_offset: float = 0.0):
        import av

        self.path = str(path)
        self.in_t, self.out_t = in_t, out_t
        self.blend = blend
        self.time_offset = time_offset   # source time = file time + offset (for pre-rendered segments)
        self.container = av.open(self.path)
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.tb = float(self.stream.time_base)
        self.width, self.height = self.stream.codec_context.width, self.stream.codec_context.height
        self._iter = None
        self.prev = None     # (t, frame)
        self.next = None
        self._seek(in_t)
        self.last = None

    def _seek(self, t: float) -> None:
        file_t = max(0.0, t - self.time_offset - 0.5)
        try:
            self.container.seek(int(file_t / self.tb), stream=self.stream, backward=True, any_frame=False)
        except Exception:
            self.container.seek(0)
        self._iter = self.container.decode(self.stream)
        self.prev = None
        self.next = None

    def _decode_next(self):
        assert self._iter is not None
        for frame in self._iter:
            if frame.pts is None:
                continue
            t = float(frame.pts * self.tb) + self.time_offset
            return t, frame
        return None

    def _to_array(self, frame) -> np.ndarray:
        return frame.to_ndarray(format="bgr24")

    def frame_at(self, s: float, rate: float = 1.0) -> np.ndarray:
        """Frame at source time s (>= previous call's s)."""
        if self.next is None:
            nxt = self._decode_next()
            if nxt is None:
                return self.last if self.last is not None else np.zeros((self.height, self.width, 3), np.uint8)
            self.next = nxt
        while self.next is not None and self.next[0] <= s + 1e-6:
            self.prev = self.next
            self.next = self._decode_next()
        if self.prev is None:
            arr = self._to_array(self.next[1]) if self.next else self.last
            self.last = arr
            return arr
        prev_t, prev_f = self.prev
        if self.blend and rate < 0.95 and self.next is not None:
            next_t, next_f = self.next
            span = next_t - prev_t
            if span > 1e-6:
                w = float(np.clip((s - prev_t) / span, 0.0, 1.0))
                if 0.02 < w < 0.98:
                    a = self._to_array(prev_f).astype(np.float32)
                    b = self._to_array(next_f).astype(np.float32)
                    arr = (a * (1 - w) + b * w + 0.5).astype(np.uint8)
                    self.last = arr
                    return arr
                if w >= 0.98:
                    arr = self._to_array(next_f)
                    self.last = arr
                    return arr
        arr = self._to_array(prev_f)
        self.last = arr
        return arr

    def close(self) -> None:
        try:
            self.container.close()
        except Exception:
            pass


def prepare_smooth(src: str | Path, in_t: float, out_t: float, dst: str | Path, target_fps: float) -> Path:
    """Pre-render [in_t, out_t] of src with ffmpeg minterpolate to target_fps (motion-compensated)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, in_t - 0.1):.3f}", "-to", f"{out_t + 0.1:.3f}", "-i", str(src),
        "-vf", f"minterpolate=fps={target_fps:.3f}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=none",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"minterpolate failed: {r.stderr.strip()[-600:]}")
    return dst

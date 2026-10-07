"""Audio for exports: silent (video only), the song window mixed in, or original clip audio."""
from __future__ import annotations

import subprocess
from pathlib import Path

from wildcut.media import MediaError, probe
from wildcut.planner.speed import timeline_duration


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"ffmpeg audio step failed: {r.stderr.strip()[-800:]}")


def build_song_audio(song_path: str, window: dict, duration: float, dst: Path, fade_out: float = 0.6) -> Path:
    """The chosen song window trimmed to the edit length, with a short fade out."""
    start = float(window["start"])
    length = min(float(window["end"]) - start, duration)
    af = f"atrim=0:{length:.3f},asetpts=PTS-STARTPTS"
    if fade_out > 0 and length > fade_out:
        af += f",afade=t=out:st={length - fade_out:.3f}:d={fade_out:.3f}"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", song_path,
           "-t", f"{length:.3f}", "-af", af, "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(dst)]
    _run(cmd)
    return dst


def build_original_audio(edl: dict, dst: Path) -> Path | None:
    """Concatenate each clip's own audio (speed-ramped clips use their average rate via atempo)."""
    parts = []
    inputs = []
    filters = []
    idx = 0
    for c in edl["clips"]:
        if not c.get("enabled", True):
            continue
        info = probe(c["src"])
        tl = timeline_duration(c["in"], c["out"], c.get("speed"))
        if not info.has_audio:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{tl:.3f},asetpts=PTS-STARTPTS[a{idx}]")
            parts.append(f"[a{idx}]")
            idx += 1
            continue
        inputs += ["-ss", f"{c['in']:.3f}", "-t", f"{c['out'] - c['in']:.3f}", "-i", c["src"]]
        src_len = c["out"] - c["in"]
        rate = src_len / tl if tl > 0 else 1.0
        chain = f"[{len(inputs) // 6 - 1}:a]aresample=48000,aformat=channel_layouts=stereo"
        r = rate
        while r < 0.5:
            chain += ",atempo=0.5"
            r /= 0.5
        while r > 2.0:
            chain += ",atempo=2.0"
            r /= 2.0
        if abs(r - 1.0) > 1e-3:
            chain += f",atempo={r:.4f}"
        chain += f",atrim=0:{tl:.3f},asetpts=PTS-STARTPTS[a{idx}]"
        filters.append(chain)
        parts.append(f"[a{idx}]")
        idx += 1
    if not parts:
        return None
    fc = ";".join(filters) + ";" + "".join(parts) + f"concat=n={len(parts)}:v=0:a=1[out]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs + [
        "-filter_complex", fc, "-map", "[out]", "-c:a", "aac", "-b:a", "160k", str(dst)]
    _run(cmd)
    return dst


def mux(video: Path, audio: Path | None, dst: Path) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if audio is not None:
        cmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy", "-shortest"]
    cmd += ["-c:v", "copy", "-movflags", "+faststart", "-fflags", "+bitexact", "-map_metadata", "-1", str(dst)]
    _run(cmd)
    return dst

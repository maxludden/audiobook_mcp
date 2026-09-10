"""
Low-level ffmpeg/ffprobe helpers shared by the alignment and encoding
stages.

IMPORTANT: nothing in this module writes to stdout. This package is used
inside an MCP stdio server, where stdout is reserved for the JSON-RPC
protocol stream -- any stray print() would corrupt the connection. All
diagnostics are returned as values or raised as exceptions; callers decide
whether to log them (typically via `ctx.info(...)` in server.py).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


class FfmpegNotFoundError(RuntimeError):
    """ffmpeg/ffprobe are not on PATH."""


class AudioProbeError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


def check_ffmpeg_available() -> None:
    """Raise FfmpegNotFoundError with an actionable message if ffmpeg or
    ffprobe are missing from PATH. Call this once, early, in any tool that
    is about to shell out to either binary."""
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise FfmpegNotFoundError(
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg "
            "(e.g. `apt install ffmpeg` / `brew install ffmpeg`) and make sure "
            "the audiobook_mcp server process can see it on PATH, then retry."
        )


def ffprobe_duration(path: Path) -> float:
    """Return the duration (seconds) of a media file via ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffprobe not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(f"ffprobe timed out probing duration of {path}") from e
    if out.returncode != 0 or not out.stdout.strip():
        raise AudioProbeError(
            f"ffprobe could not read duration of {path}. It may not be a "
            f"valid audio file. ffprobe stderr: {out.stderr.strip()[-500:]}"
        )
    try:
        return float(out.stdout.strip())
    except ValueError as e:
        raise AudioProbeError(f"ffprobe returned a non-numeric duration for {path}: "
                               f"{out.stdout.strip()!r}") from e


def detect_silences(path: Path, start: float, dur: float,
                     noise_db: float = -30, min_silence: float = 0.35) -> list[tuple[float, float]]:
    """Run ffmpeg's silencedetect filter over [start, start+dur) and return
    a list of (abs_start, abs_end) tuples for every silent interval found
    that is at least `min_silence` seconds long and at least `noise_db`
    quiet. Both endpoints are absolute offsets (seconds) into the source
    file, not relative to `start`.
    """
    start = max(0.0, start)
    if dur <= 0:
        return []
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffmpeg not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(
            f"ffmpeg silencedetect timed out over [{start:.1f}, {start + dur:.1f}) of {path}"
        ) from e
    log = proc.stderr
    # ffmpeg's silencedetect filter reports timestamps relative to the
    # `-ss` seek point we just gave it, not the original file -- convert
    # to absolute offsets here, once, so every caller can treat this
    # function's return value as absolute (as documented above) without
    # having to remember to add `start` back in themselves.
    starts = [start + float(m.group(1)) for m in _SILENCE_START.finditer(log)]
    ends = [(start + float(m.group(1)), float(m.group(2))) for m in _SILENCE_END.finditer(log)]
    intervals = []
    for i, s in enumerate(starts):
        if i < len(ends):
            e, _d = ends[i]
            intervals.append((s, e))
    return intervals


def render_waveform_png(path: Path, center: float, half_window: float, out_path: Path,
                         width: int = 1200, height: int = 200) -> Path:
    """Render a waveform image spanning [center - half_window, center +
    half_window] to out_path (PNG). Useful for visually confirming a
    chapter-boundary cut lands in a clean gap rather than mid-sentence, per
    the skill's "companion trick" for spot-checking boundaries.
    """
    start = max(0.0, center - half_window)
    dur = 2 * half_window
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
        "-filter_complex", f"showwavespic=s={width}x{height}:colors=white",
        "-frames:v", "1", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffmpeg not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(f"ffmpeg timed out rendering waveform for {path}") from e
    if proc.returncode != 0 or not out_path.exists():
        raise AudioProbeError(
            f"ffmpeg failed to render waveform image. stderr: {proc.stderr.strip()[-500:]}"
        )
    return out_path

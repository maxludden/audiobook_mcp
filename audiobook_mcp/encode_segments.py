"""
Encode one aligned chapter segment of the source audio to its own AAC
(.m4a) file. Pure library code -- the resumable batching lives in
pipeline.py's stage_encode, which decides how many segments to encode per
call.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .audio_probe import FfmpegNotFoundError, AudioProbeError


def safe_name(n: int, title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60]
    return f"{n:03d}_{slug}.m4a"


def encode_one(src: Path, start: float, end: float, out_path: Path,
                bitrate: str = "96k", channels: int = 1, sample_rate: int = 32000) -> None:
    """Cut [start, end) out of `src` and encode it to AAC at out_path."""
    dur = end - start
    if dur <= 0:
        raise AudioProbeError(f"Refusing to encode a non-positive-duration segment "
                               f"({start:.3f} -> {end:.3f}) into {out_path.name}")
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
        "-c:a", "aac", "-b:a", bitrate, "-ac", str(channels), "-ar", str(sample_rate),
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffmpeg not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(f"ffmpeg timed out encoding segment {out_path.name}") from e
    except subprocess.CalledProcessError as e:
        raise AudioProbeError(
            f"ffmpeg failed encoding segment {out_path.name}: {e.stderr.strip()[-500:]}"
        ) from e

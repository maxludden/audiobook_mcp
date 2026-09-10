"""
Verification checks for a finished M4B, per the skill's "Verifying the
result" section: chapter count, total duration (vs. the source file, if
given), first/last chapter titles, and cover-art presence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .audio_probe import FfmpegNotFoundError, AudioProbeError, ffprobe_duration


def _ffprobe_json(path: Path) -> dict:
    if not path.exists():
        raise AudioProbeError(f"File not found: {path}")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_chapters", "-show_streams",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffprobe not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(f"ffprobe timed out probing {path}") from e
    if proc.returncode != 0:
        raise AudioProbeError(
            f"ffprobe couldn't read {path} -- is it a valid M4B/MP4? "
            f"stderr: {proc.stderr.strip()[-500:]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AudioProbeError(f"ffprobe returned unparseable output for {path}") from e


def verify_m4b(m4b_path: Path, source_audio_path: Optional[Path],
               expected_chapter_count: Optional[int]) -> dict:
    data = _ffprobe_json(m4b_path)
    fmt = data.get("format", {})
    chapters = data.get("chapters", [])
    streams = data.get("streams", [])

    duration = float(fmt.get("duration", 0.0))
    has_cover = any(
        s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1
        for s in streams
    )

    issues: list[str] = []
    if not chapters:
        issues.append("No chapters found in the output file.")
    if expected_chapter_count is not None and len(chapters) != expected_chapter_count:
        issues.append(
            f"Chapter count mismatch: file has {len(chapters)}, expected {expected_chapter_count} "
            "(this is fine if an intro/outro was intentionally detected -- otherwise a chapter "
            "may have been merged or split; check word_count_warnings from audiobook_get_status)."
        )
    if not has_cover:
        issues.append("No cover-art (attached_pic) video stream found.")

    result = {
        "duration_seconds": round(duration, 3),
        "chapter_count": len(chapters),
        "first_chapter_title": _chapter_title(chapters[0]) if chapters else None,
        "last_chapter_title": _chapter_title(chapters[-1]) if chapters else None,
        "has_cover_art": has_cover,
    }

    if source_audio_path is not None:
        src_duration = ffprobe_duration(source_audio_path)
        diff = duration - src_duration
        result["source_duration_seconds"] = round(src_duration, 3)
        result["duration_difference_seconds"] = round(diff, 3)
        if abs(diff) > 2.0:
            issues.append(
                f"Output duration differs from source by {diff:+.1f}s (expected ~0 -- large "
                "differences usually mean the deliver stage is incomplete, or intro/outro "
                "detection unexpectedly dropped/kept audio)."
            )

    result["issues"] = issues
    return result


def _chapter_title(chapter: dict) -> str:
    return chapter.get("tags", {}).get("title", "")

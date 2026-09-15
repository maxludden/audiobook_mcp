"""
Transcribe a short clip of chapter-opening audio via whisper.cpp, to help
judge a low-confidence/fallback chapter boundary the way
audiobook_render_boundary_waveform's waveform image does visually, but as
text: a transcript that trails off mid-sentence from the *previous*
chapter means the boundary landed too late; one missing this chapter's
first words means it landed too early.

Dependency-free by design, matching how ffmpeg/exiftool are used
elsewhere in this package: whisper.cpp
(https://github.com/ggerganov/whisper.cpp) is expected as an external
binary on PATH (or pointed to explicitly via AUDIOBOOK_MCP_WHISPER_BIN),
plus a downloaded GGML model file pointed to by
AUDIOBOOK_MCP_WHISPER_MODEL -- no Python speech-recognition dependency,
no model bundled or auto-downloaded by this package.

Output is read from whisper.cpp's own `-oj`/`--output-json` file rather
than scraped from its stdout: the JSON schema (a "transcription" list of
{"text": ...} segments) is documented and stable, whereas stdout's
formatting depends on which print-suppression flags a given build
supports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .audio_probe import AudioProbeError, FfmpegNotFoundError, check_ffmpeg_available

_BIN_CANDIDATES = ("whisper-cli", "whisper-cpp")


class WhisperCppNotFoundError(RuntimeError):
    """whisper.cpp's binary or model isn't available/configured."""


class TranscribeError(RuntimeError):
    """whisper.cpp (or the ffmpeg clip extraction ahead of it) ran but failed."""


def _find_binary() -> str:
    override = os.environ.get("AUDIOBOOK_MCP_WHISPER_BIN")
    if override:
        if shutil.which(override):
            return override
        raise WhisperCppNotFoundError(
            f"AUDIOBOOK_MCP_WHISPER_BIN is set to {override!r}, but that isn't an executable "
            "on PATH. Fix the path, or unset it to auto-detect "
            f"{'/'.join(_BIN_CANDIDATES)} instead."
        )
    for name in _BIN_CANDIDATES:
        if shutil.which(name):
            return name
    raise WhisperCppNotFoundError(
        f"No whisper.cpp binary found on PATH (tried {', '.join(_BIN_CANDIDATES)}). Build/install "
        "whisper.cpp (https://github.com/ggerganov/whisper.cpp) and either put its CLI binary on "
        "PATH or set AUDIOBOOK_MCP_WHISPER_BIN to its full path."
    )


def _find_model() -> Path:
    model = os.environ.get("AUDIOBOOK_MCP_WHISPER_MODEL")
    if not model:
        raise WhisperCppNotFoundError(
            "AUDIOBOOK_MCP_WHISPER_MODEL is not set. Point it at a downloaded GGML model file "
            "(e.g. ggml-base.en.bin -- see whisper.cpp's models/download-ggml-model.sh) before "
            "using audiobook_transcribe_chapter_boundary."
        )
    path = Path(model)
    if not path.is_file():
        raise WhisperCppNotFoundError(f"AUDIOBOOK_MCP_WHISPER_MODEL points to {path}, which doesn't exist.")
    return path


def check_whispercpp_available() -> tuple[str, Path]:
    """Raise WhisperCppNotFoundError with an actionable message if
    whisper.cpp's binary or model aren't available/configured. Returns
    (binary, model_path) on success."""
    return _find_binary(), _find_model()


def transcribe_clip(src: Path, start: float, duration: float, out_wav: Path,
                     language: str = "en") -> str:
    """Extract [start, start+duration) of `src` to a 16kHz mono WAV at
    out_wav (whisper.cpp's documented input format) and transcribe it.
    Returns the transcript text -- possibly empty if whisper.cpp found no
    speech in the clip (e.g. a boundary that lands in the middle of
    silence), which is not itself an error."""
    check_ffmpeg_available()
    binary, model = check_whispercpp_available()

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav),
    ]
    try:
        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise FfmpegNotFoundError("ffmpeg not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise AudioProbeError(f"ffmpeg timed out extracting a clip from {src}") from e
    if proc.returncode != 0 or not out_wav.exists():
        raise AudioProbeError(
            f"ffmpeg failed extracting the clip to transcribe. stderr: {proc.stderr.strip()[-500:]}"
        )

    json_base = out_wav.with_suffix("")  # whisper.cpp appends its own extension for -oj
    result_path = json_base.with_suffix(".json")
    whisper_cmd = [binary, "-m", str(model), "-f", str(out_wav), "-l", language,
                   "-oj", "-of", str(json_base)]
    try:
        proc = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise WhisperCppNotFoundError(f"{binary!r} could not be executed.") from e
    except subprocess.TimeoutExpired as e:
        raise TranscribeError(f"whisper.cpp timed out transcribing {out_wav}") from e
    if proc.returncode != 0:
        raise TranscribeError(f"whisper.cpp failed: {proc.stderr.strip()[-500:]}")
    if not result_path.exists():
        raise TranscribeError(
            f"whisper.cpp exited cleanly but didn't produce the expected output at {result_path}. "
            f"stderr: {proc.stderr.strip()[-500:]}"
        )

    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError as e:
        raise TranscribeError(f"whisper.cpp produced unparseable JSON output at {result_path}") from e

    segments = data.get("transcription", [])
    return " ".join(seg.get("text", "").strip() for seg in segments).strip()

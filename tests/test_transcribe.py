"""
Unit tests for the whisper.cpp availability checks (no real binary/model
needed -- shutil.which is monkeypatched) and the EPUB opening-text helper,
plus a real end-to-end transcription test that's skipped unless a
whisper.cpp binary and model are actually configured in this environment
(matching test_tool_integration.py's `pytest.skip` pattern for exiftool).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from audiobook_mcp import transcribe
from audiobook_mcp.epub_extract import chapter_opening_text
from audiobook_mcp.pipeline import Pipeline, PipelineConfig


def _stub_which(available: str | None):
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == available else None
    return which


def test_missing_binary_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_BIN", raising=False)
    monkeypatch.setattr(transcribe.shutil, "which", _stub_which(None))
    with pytest.raises(transcribe.WhisperCppNotFoundError, match="whisper.cpp"):
        transcribe.check_whispercpp_available()


def test_bad_binary_override_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIOBOOK_MCP_WHISPER_BIN", "definitely-not-a-real-binary-xyz")
    monkeypatch.setattr(transcribe.shutil, "which", _stub_which(None))
    with pytest.raises(transcribe.WhisperCppNotFoundError, match="AUDIOBOOK_MCP_WHISPER_BIN"):
        transcribe.check_whispercpp_available()


def test_missing_model_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_BIN", raising=False)
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(transcribe.shutil, "which", _stub_which("whisper-cli"))
    with pytest.raises(transcribe.WhisperCppNotFoundError, match="AUDIOBOOK_MCP_WHISPER_MODEL"):
        transcribe.check_whispercpp_available()


def test_nonexistent_model_path_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_BIN", raising=False)
    monkeypatch.setattr(transcribe.shutil, "which", _stub_which("whisper-cli"))
    monkeypatch.setenv("AUDIOBOOK_MCP_WHISPER_MODEL", str(tmp_path / "nope.bin"))
    with pytest.raises(transcribe.WhisperCppNotFoundError, match="doesn't exist"):
        transcribe.check_whispercpp_available()


def test_available_when_binary_and_model_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AUDIOBOOK_MCP_WHISPER_BIN", raising=False)
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"fake-model-bytes")
    monkeypatch.setattr(transcribe.shutil, "which", _stub_which("whisper-cli"))
    monkeypatch.setenv("AUDIOBOOK_MCP_WHISPER_MODEL", str(model))
    binary, model_path = transcribe.check_whispercpp_available()
    assert binary == "whisper-cli"
    assert model_path == model


def test_chapter_opening_text_excludes_heading(tmp_path: Path) -> None:
    chapter_html = tmp_path / "ch1.xhtml"
    chapter_html.write_text(
        "<html><body><h1>Chapter One</h1><p>It was a dark and stormy night in the old house.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    text = chapter_opening_text(chapter_html, n_words=5)
    assert text == "It was a dark and"
    assert "Chapter One" not in text


def test_chapter_opening_text_without_heading_falls_back_to_full_text(tmp_path: Path) -> None:
    chapter_html = tmp_path / "ch1.xhtml"
    chapter_html.write_text("<html><body><p>No heading here at all.</p></body></html>", encoding="utf-8")
    text = chapter_opening_text(chapter_html, n_words=3)
    assert text == "No heading here"


def test_chapter_href_resolves_against_extraction_root(fixture_dir: Path) -> None:
    """chapter['_href'] must be joinable as work_dir/"epub"/_href -- every
    caller (this test's own end-to-end test below, plus
    audiobook_transcribe_chapter_boundary and
    Pipeline._verify_first_boundary) does exactly that join. The OPF file
    lives inside a subdirectory (e.g. "OEBPS/") for this fixture, same as
    virtually every real-world EPUB, so this catches _href being returned
    relative to the OPF's own directory instead of the extraction root."""
    mp3 = fixture_dir / "fixture_book.mp3"
    epub = fixture_dir / "fixture_book.epub"
    out_dir = fixture_dir / "href_check_out"
    work_dir = fixture_dir / "href_check_work"

    config = PipelineConfig(mp3=mp3, epub=epub, out_dir=out_dir, min_gap=3.0)
    pipeline = Pipeline(config, work_dir)
    pipeline.run(30.0)  # extract stage only

    chapters = pipeline.state["epub"]["chapters"]
    assert chapters
    for chapter in chapters:
        href_path = pipeline.work_dir / "epub" / chapter["_href"]
        assert href_path.exists(), f"chapter {chapter['n']}'s _href doesn't resolve: {href_path}"


def test_real_transcription_end_to_end(fixture_dir: Path) -> None:
    """Exercises the real ffmpeg-clip-extraction + whisper.cpp subprocess
    wiring (the same transcribe_clip() the MCP tool calls) against the
    synthetic fixture. Skipped unless whisper.cpp is actually configured
    -- the fixture audio is synthesized tones, not speech, so this only
    asserts the plumbing runs cleanly end to end, not on transcript
    content."""
    have_binary = bool(os.environ.get("AUDIOBOOK_MCP_WHISPER_BIN")) or any(
        shutil.which(name) for name in transcribe._BIN_CANDIDATES
    )
    have_model = bool(os.environ.get("AUDIOBOOK_MCP_WHISPER_MODEL"))
    if not (have_binary and have_model):
        pytest.skip("whisper.cpp binary/model not configured in this environment")

    mp3 = fixture_dir / "fixture_book.mp3"
    epub = fixture_dir / "fixture_book.epub"
    out_dir = fixture_dir / "transcribe_out"
    work_dir = fixture_dir / "transcribe_work"

    config = PipelineConfig(mp3=mp3, epub=epub, out_dir=out_dir, min_gap=3.0)
    PipelineConfig.validate_new(mp3, epub, out_dir)
    pipeline = Pipeline(config, work_dir)

    status = pipeline.summarize()
    loops = 0
    while status.get("alignment", {}).get("chapters_aligned", 0) < 2 and not status["done"]:
        status = pipeline.run(30.0)
        loops += 1
        assert loops < 10, status

    boundary = pipeline.state["align"]["boundaries"]["2"]
    out_wav = work_dir / "transcripts" / "probe.wav"
    transcript = transcribe.transcribe_clip(mp3, boundary, 5.0, out_wav)
    assert isinstance(transcript, str)
    assert out_wav.exists()

    chapter = next(c for c in pipeline.state["epub"]["chapters"] if c["n"] == 2)
    href_path = work_dir / "epub" / chapter["_href"]
    expected = chapter_opening_text(href_path)
    assert expected  # the fixture's chapter body is non-empty

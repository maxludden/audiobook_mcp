"""
Unit tests for verify_first_chapter -- confirming chapter 1's boundary by
transcript before the forward-chaining alignment loop runs (see
pipeline.py's module docstring for why chapter 1 is the one boundary a
bad guess can't be contained to). whisper.cpp itself is mocked out via
transcribe.transcribe_clip so these run without a real binary/model
configured; tests/test_transcribe.py covers the real subprocess wiring.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from audiobook_mcp import pipeline as pipeline_module
from audiobook_mcp import transcribe
from audiobook_mcp.epub_extract import chapter_opening_text
from audiobook_mcp.pipeline import Pipeline, PipelineConfig, _matches_chapter_opening


def test_matches_chapter_opening_exact_match() -> None:
    text = "It was a dark and stormy night when the door creaked open."
    assert _matches_chapter_opening(text, text)


def test_matches_chapter_opening_tolerates_asr_noise() -> None:
    # narrator-spoken "chapter one" label + a couple of misheard words
    transcript = "chapter one it was a dark and stormy knight when the door creaked open"
    expected = "It was a dark and stormy night when the door creaked open"
    assert _matches_chapter_opening(transcript, expected)


def test_matches_chapter_opening_rejects_unrelated_intro() -> None:
    transcript = "this audiobook is narrated by jane doe unabridged recording copyright"
    expected = "It was a dark and stormy night when the door creaked open"
    assert not _matches_chapter_opening(transcript, expected)


def test_matches_chapter_opening_rejects_empty_transcript() -> None:
    assert not _matches_chapter_opening("", "It was a dark and stormy night")


def _pipeline_at_align(fixture_dir: Path, name: str) -> Pipeline:
    """Run just the extract stage so align/epub state exists, without
    touching the real detect_silences-based candidate search -- tests
    seed align['anchor_check']['candidates'] themselves."""
    mp3 = fixture_dir / "fixture_book.mp3"
    epub = fixture_dir / "fixture_book.epub"
    config = PipelineConfig(mp3=mp3, epub=epub, out_dir=fixture_dir / f"{name}_out", min_gap=3.0)
    pipeline = Pipeline(config, fixture_dir / f"{name}_work")
    pipeline.run(30.0)
    assert pipeline.state["stage"] == "align"
    return pipeline


def test_verify_first_boundary_confirms_matching_candidate(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = _pipeline_at_align(fixture_dir, "anchor_confirm")
    chapters = pipeline.state["epub"]["chapters"]
    expected = chapter_opening_text(pipeline.work_dir / "epub" / chapters[0]["_href"])
    assert expected

    align = pipeline.state["align"]
    align["anchor_check"] = {"candidates": [12.0, 47.0, 90.0], "tried": [], "next_index": 0}
    pipeline._save()

    def fake_transcribe(mp3_path, start, duration, out_wav, language="en"):
        return expected if start == 47.0 else "please enjoy this audiobook presented by our narrator"

    monkeypatch.setattr(transcribe, "transcribe_clip", fake_transcribe)

    total_dur = pipeline.state["mp3_duration"]
    assert pipeline._verify_first_boundary(30.0, total_dur, cb=None) is True
    assert align["boundaries"]["1"] == 47.0
    assert align["anchor_check"]["status"] == "confirmed"
    assert len(align["anchor_check"]["tried"]) == 2  # stopped as soon as 47.0 matched


def test_verify_first_boundary_falls_back_when_no_candidate_matches(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = _pipeline_at_align(fixture_dir, "anchor_no_match")
    align = pipeline.state["align"]
    align["anchor_check"] = {"candidates": [12.0, 47.0], "tried": [], "next_index": 0}
    pipeline._save()

    monkeypatch.setattr(
        transcribe, "transcribe_clip",
        lambda mp3_path, start, duration, out_wav, language="en": "completely unrelated filler text",
    )

    total_dur = pipeline.state["mp3_duration"]
    assert pipeline._verify_first_boundary(30.0, total_dur, cb=None) is True
    assert align["anchor_check"]["status"] == "unverified"
    assert align["boundaries"]["1"] == 0.0  # detect_intro defaults to False
    # both candidates should actually have been transcribed and compared
    # (not an early bail-out for some other reason, e.g. missing EPUB text)
    assert len(align["anchor_check"]["tried"]) == 2
    assert all(not t["matched"] for t in align["anchor_check"]["tried"])


def test_verify_first_boundary_falls_back_when_whisper_unavailable(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = _pipeline_at_align(fixture_dir, "anchor_no_whisper")
    align = pipeline.state["align"]
    align["anchor_check"] = {"candidates": [12.0], "tried": [], "next_index": 0}
    pipeline._save()

    def raise_missing(mp3_path, start, duration, out_wav, language="en"):
        raise transcribe.WhisperCppNotFoundError("no whisper.cpp configured")

    monkeypatch.setattr(transcribe, "transcribe_clip", raise_missing)

    total_dur = pipeline.state["mp3_duration"]
    assert pipeline._verify_first_boundary(30.0, total_dur, cb=None) is True
    assert align["anchor_check"]["status"] == "unverified"
    assert align["boundaries"]["1"] == 0.0


def test_verify_first_boundary_gives_up_without_epub_text(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = _pipeline_at_align(fixture_dir, "anchor_no_epub_text")
    monkeypatch.setattr(pipeline_module, "chapter_opening_text", lambda *a, **kw: "")

    total_dur = pipeline.state["mp3_duration"]
    assert pipeline._verify_first_boundary(30.0, total_dur, cb=None) is True
    align = pipeline.state["align"]
    assert align["anchor_check"]["status"] == "unverified"
    assert align["boundaries"]["1"] == 0.0


def test_verify_first_boundary_resumes_across_budget_limited_calls(
    fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = _pipeline_at_align(fixture_dir, "anchor_resume")
    align = pipeline.state["align"]
    align["anchor_check"] = {"candidates": [12.0, 47.0, 90.0], "tried": [], "next_index": 0}
    pipeline._save()

    calls: list[float] = []

    def slow_transcribe(mp3_path, start, duration, out_wav, language="en"):
        calls.append(start)
        return "still nothing like the chapter's actual opening text at all"

    monkeypatch.setattr(transcribe, "transcribe_clip", slow_transcribe)

    total_dur = pipeline.state["mp3_duration"]
    # a budget of 0 lets the loop's time check fail immediately, so no
    # candidate is tried yet and the call reports "not done" for a resume
    assert pipeline._verify_first_boundary(0.0, total_dur, cb=None) is False
    assert calls == []
    assert "1" not in align["boundaries"]

    assert pipeline._verify_first_boundary(30.0, total_dur, cb=None) is True
    assert calls == [12.0, 47.0, 90.0]
    assert align["anchor_check"]["status"] == "unverified"
    assert align["boundaries"]["1"] == 0.0


def test_confirmed_intro_boundary_produces_opening_credits_segment(fixture_dir: Path) -> None:
    """_build_segments' own "meaningful intro" threshold
    (boundaries['1'] > 5.0) is independent of detect_intro -- once
    verify_first_chapter confirms a non-zero chapter-1 start, this is
    what turns that into an actual Opening Credits segment in the final
    output. Runs the fixture to completion first (boundaries['1'] == 0.0
    there -- no real intro), then substitutes a confirmed-anchor-style
    non-zero value and rebuilds segments directly, since
    _verify_first_boundary's own candidate search is covered separately
    above."""
    pipeline = _pipeline_at_align(fixture_dir, "anchor_intro_segment")
    status = pipeline.summarize()
    loops = 0
    while not status["done"]:
        status = pipeline.run(30.0)
        loops += 1
        assert loops < 50, status

    align = pipeline.state["align"]
    fake_intro_end = 6.0
    assert 5.0 < fake_intro_end < align["boundaries"]["2"], (
        "fixture's chapter 1 changed length -- pick a fake_intro_end that still "
        f"fits between 5.0 and boundaries['2']={align['boundaries']['2']}"
    )
    align["boundaries"]["1"] = fake_intro_end
    pipeline._build_segments()

    segments = pipeline.state["segments"]
    assert segments[0]["title"] == pipeline.config.intro_title
    assert segments[0]["start"] == 0.0
    assert segments[0]["end"] == fake_intro_end
    assert segments[1]["title"] == pipeline.state["epub"]["chapters"][0]["title"]
    assert segments[1]["start"] == fake_intro_end

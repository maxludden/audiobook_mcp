"""
End-to-end smoke test: run the actual Pipeline (not through the MCP
protocol -- just the library code every tool in server.py calls) against
the synthetic fixture from evaluations/make_fixture.py, and assert the
output is a valid, correctly-chaptered M4B.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from audiobook_mcp.audio_probe import detect_silences
from audiobook_mcp.pipeline import Pipeline, PipelineConfig
from audiobook_mcp.verify import verify_m4b


def test_full_pipeline_smoke(fixture_dir: Path) -> None:
    mp3 = fixture_dir / "fixture_book.mp3"
    epub = fixture_dir / "fixture_book.epub"
    out_dir = fixture_dir / "out"
    work_dir = fixture_dir / "work"

    config = PipelineConfig(mp3=mp3, epub=epub, out_dir=out_dir, min_gap=3.0)
    PipelineConfig.validate_new(mp3, epub, out_dir)
    pipeline = Pipeline(config, work_dir)

    logs: list[str] = []
    loops = 0
    status = pipeline.summarize()
    while not status["done"]:
        status = pipeline.run(30.0, cb=logs.append)
        loops += 1
        assert loops < 50, f"pipeline didn't converge after {loops} chunks: {status}"

    assert status["stage"] == "done"
    assert status["book"]["chapter_count"] == 4, status["book"]
    assert status["book"]["title"] == "The Fixture Chronicles"
    assert status["book"]["cover_found"] is True, status["book"]
    assert "final_output" in status
    final_output = Path(status["final_output"])
    assert final_output.exists(), f"missing output file: {final_output}"

    # The transition into chapter 4 has a real ~4s gap, a brief
    # announcement-like blip, then a ~2s decoy gap a few seconds later.
    # The pipeline must anchor on the *first* qualifying gap (the real
    # one), not the decoy. Confirm by independently re-scanning that
    # region for every silence interval and checking boundary 4 matches
    # the earlier (real) one, not the later (decoy) one.
    align = pipeline.state["align"]
    assert not align["fallback"], f"unexpected fallback chapters: {align['fallback']}"
    b3, b4 = align["boundaries"]["3"], align["boundaries"]["4"]
    # search from b3+3s, same as the pipeline's own _find_gap, so we don't
    # pick up the tail end of the *previous* (2->3) silence gap that b3
    # itself sits in the middle of
    search_start = b3 + 3.0
    gaps = detect_silences(mp3, search_start, (b4 - search_start) + 10.0, noise_db=-30, min_silence=1.5)
    assert len(gaps) >= 2, f"expected to independently re-find both the real and decoy gaps, got {gaps}"
    gap_mids = sorted((s + e) / 2 for s, e in gaps)
    real_gap_mid, decoy_gap_mid = gap_mids[0], gap_mids[1]
    assert abs(b4 - real_gap_mid) < 0.5, (
        f"chapter 4 boundary {b4} should match the real gap's midpoint {real_gap_mid} "
        f"(decoy gap's midpoint was {decoy_gap_mid}) -- looks like decoy filtering picked the wrong gap"
    )
    assert abs(b4 - decoy_gap_mid) > 1.5, (
        f"chapter 4 boundary {b4} landed suspiciously close to the decoy gap {decoy_gap_mid}"
    )

    verify = verify_m4b(final_output, mp3, expected_chapter_count=4)
    assert verify["chapter_count"] == 4
    assert abs(verify["duration_difference_seconds"]) < 1.0, verify
    assert not verify["issues"], verify["issues"]

    # ffprobe cross-check independent of our own verify() code
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(final_output)],
        capture_output=True, text=True, check=True,
    )
    chapters = json.loads(probe.stdout)["chapters"]
    titles = [c["tags"]["title"] for c in chapters]
    # epub_extract strips the leading "N. " numbering from TOC labels
    assert titles == ["The Beginning", "A Complication", "The Decoy Gap", "Resolution"], titles

    # exercise patch_boundary(): nudge chapter 2 by a fraction of a second
    # and confirm downstream state gets invalidated and regenerates cleanly
    old_stage = pipeline.state["stage"]
    assert old_stage == "done"
    patch_result = pipeline.patch_boundary(2, align["boundaries"]["2"] + 0.25)
    assert "segments" in patch_result["invalidated"]
    assert "final_output" in patch_result["invalidated"]
    assert pipeline.state["stage"] == "encode"
    assert 2 in pipeline.state["align"]["manual_overrides"]

    status = pipeline.summarize()
    while not status["done"]:
        status = pipeline.run(30.0, cb=logs.append)
    assert status["stage"] == "done"
    assert Path(status["final_output"]).exists()


def test_cover_override(fixture_dir: Path) -> None:
    """A cover_override path should end up embedded in the final M4B
    instead of the EPUB's own cover -- exercises the same code path as
    audiobook_start_conversion's cover_image_path parameter."""
    mp3 = fixture_dir / "fixture_book.mp3"
    epub = fixture_dir / "fixture_book.epub"
    override_cover = Path(__file__).resolve().parent.parent / "evaluations" / "assets" / "cover.png"
    out_dir = fixture_dir / "cover_override_out"
    work_dir = fixture_dir / "cover_override_work"

    config = PipelineConfig(mp3=mp3, epub=epub, out_dir=out_dir, min_gap=3.0,
                             cover_override=override_cover)
    PipelineConfig.validate_new(mp3, epub, out_dir, cover_override=override_cover)
    pipeline = Pipeline(config, work_dir)

    status = pipeline.summarize()
    while not status["done"]:
        status = pipeline.run(30.0)

    assert pipeline.state["epub"]["cover"] == str(override_cover)
    final_output = Path(status["final_output"])
    verify = verify_m4b(final_output, mp3, expected_chapter_count=4)
    assert verify["has_cover_art"] is True, verify

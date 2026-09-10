#!/usr/bin/env python3
"""
One-time setup for the evaluation set: build the synthetic fixture book,
run the real Pipeline (the same code server.py's tools call) to
completion, and register the job in the real Registry -- so the 10
evaluation questions in evaluation.xml can be answered using ONLY
read-only tools (audiobook_inspect_epub, audiobook_get_status,
audiobook_list_conversions, audiobook_inspect_chapter_boundary,
audiobook_verify_output) against data that already exists on disk, the
same way a live-service MCP evaluation explores a pre-existing account
instead of creating its own fixtures via write calls.

Run once before evaluating:
    python3 evaluations/setup_eval_fixture.py

Uses fixed, documented paths (see EVAL_SOURCE_DIR/EVAL_OUT_DIR below) so
job_id is reproducible run to run (as long as $AUDIOBOOK_MCP_HOME's
registry.json doesn't already have a *different* job for these same
paths -- delete ~/.audiobook_mcp/registry.json first if re-running from
scratch is important).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluations.make_fixture import build_epub, build_audio  # noqa: E402
from audiobook_mcp.pipeline import Pipeline, PipelineConfig  # noqa: E402
from audiobook_mcp.registry import Registry  # noqa: E402

EVAL_SOURCE_DIR = Path("/tmp/audiobook_mcp_eval/source")
EVAL_OUT_DIR = Path("/tmp/audiobook_mcp_eval/out")


def main() -> None:
    EVAL_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    epub_path = EVAL_SOURCE_DIR / "fixture_book.epub"
    mp3_path = EVAL_SOURCE_DIR / "fixture_book.mp3"
    build_epub(epub_path)
    build_audio(mp3_path)

    PipelineConfig.validate_new(mp3_path, epub_path, EVAL_OUT_DIR)
    registry = Registry()
    job_id, work_dir = registry.create_job(
        mp3=mp3_path, epub=epub_path, out_dir=EVAL_OUT_DIR, title_hint=epub_path.stem
    )
    config = PipelineConfig(mp3=mp3_path, epub=epub_path, out_dir=EVAL_OUT_DIR, min_gap=3.0)
    pipeline = Pipeline(config, work_dir)

    status = pipeline.summarize()
    loops = 0
    while not status["done"]:
        status = pipeline.run(30.0)
        loops += 1
        assert loops < 50, f"did not converge: {status}"

    print(f"job_id: {job_id}")
    print(f"epub_path: {epub_path}")
    print(f"mp3_path: {mp3_path}")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()

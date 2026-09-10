from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluations"))
from make_fixture import build_audio, build_epub  # noqa: E402


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the synthetic 4-chapter book (EPUB + MP3 with real silence
    gaps, including a decoy gap) once per test session."""
    out_dir = tmp_path_factory.mktemp("audiobook_fixture")
    build_epub(out_dir / "fixture_book.epub")
    build_audio(out_dir / "fixture_book.mp3")
    return out_dir

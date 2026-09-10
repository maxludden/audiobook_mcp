#!/usr/bin/env python3
"""
Tests for metadata_fetch.py's reconciliation/ranking logic and
cover_embed.py's exiftool embedding, using canned API-response fixtures
instead of live network calls.

Why fixtures instead of live calls: this project's build/test sandbox
doesn't have egress to googleapis.com / openlibrary.org / api.audible.com
/ itunes.apple.com (see network_configuration), so query_google_books()
etc. can't be exercised here directly. The reconciliation/ranking logic
those functions feed into is exactly what the source skill's SKILL.md
"Gotchas" section documents real failure modes for (series volume
mismatches, far-future re-issues, publisher/date conflicts) -- these
fixtures are built from the shapes described there. Before relying on
this in production, also run a handful of real lookups
(audiobook_lookup_book_metadata) somewhere with network access to the
four APIs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from audiobook_mcp import cover_embed
from audiobook_mcp import metadata_fetch as mf


def test_pick_best_prefers_exact_title_match():
    results = [{"title": "Defiance of the Fall 12"}, {"title": "Defiance of the Fall"}]
    best = mf.pick_best(results, "Defiance of the Fall", lambda r: r["title"])
    assert best["title"] == "Defiance of the Fall", best


def test_pick_best_matches_series_volume_not_relevance_rank():
    # Mirrors the skill's documented failure mode: an unfiltered [0] for
    # "Defiance of the Fall" + author returns book 12 first.
    results = [
        {"title": "Defiance of the Fall 12 (Unabridged)"},
        {"title": "Defiance of the Fall 6 (Unabridged)"},
        {"title": "Defiance of the Fall (Unabridged)"},  # unnumbered = volume 1
    ]
    best = mf.pick_best(results, "Defiance of the Fall 6", lambda r: r["title"])
    assert best["title"] == "Defiance of the Fall 6 (Unabridged)", best


def test_pick_best_volume_1_matches_unnumbered_title():
    results = [{"title": "Defiance of the Fall (Unabridged)"},
               {"title": "Defiance of the Fall 2 (Unabridged)"}]
    best = mf.pick_best(results, "Defiance of the Fall 1", lambda r: r["title"])
    assert best["title"] == "Defiance of the Fall (Unabridged)", best


def test_pick_best_refuses_to_guess_a_missing_volume():
    # Requested volume genuinely isn't in the result set -- must return
    # None, not silently hand back a different book.
    results = [{"title": "Defiance of the Fall 1"}, {"title": "Defiance of the Fall 2"}]
    best = mf.pick_best(results, "Defiance of the Fall 9", lambda r: r["title"])
    assert best is None, best


def test_pick_best_drops_far_future_reissues():
    results = [{"title": "Project Hail Mary", "date": "2026-11-01"},
               {"title": "Project Hail Mary", "date": "2021-05-04"}]
    best = mf.pick_best(results, "Project Hail Mary", lambda r: r["title"],
                         lambda r: r["date"], current_year=2024)
    assert best["date"] == "2021-05-04", best


def test_upsize_apple_artwork():
    assert mf.upsize_apple_artwork("https://x.mzstatic.com/img/100x100bb.jpg") == \
        "https://x.mzstatic.com/img/2400x2400bb.jpg"


def test_reconcile_flags_publisher_conflict():
    # The skill's most common documented conflict: audiobook publisher
    # (Audible) vs. print publisher (Open Library) for the same title.
    out = {
        "audible": [{"title": "Project Hail Mary", "authors": [{"name": "Andy Weir"}],
                      "narrators": [{"name": "Ray Porter"}], "publisher_name": "Audible Studios",
                      "release_date": "2021-05-04", "runtime_length_min": 970,
                      "asin": "B08G9PRS1K"}],
        "openlibrary": [{"title": "Project Hail Mary", "author_name": ["Andy Weir"],
                          "publisher": ["Penguin Random House"], "first_publish_year": 2021,
                          "ratings_average": 4.6, "ratings_count": 1200, "isbn": ["9780593135204"]}],
        "google": [], "apple": [],
    }
    rec = mf.reconcile(out, "Project Hail Mary")
    assert rec["narrators"] == ["Ray Porter"], rec
    assert rec["length"] == "16h 10m", rec
    assert rec["_provenance"]["publisher"] == "audible", rec
    assert any(c.startswith("publisher:") for c in rec["_conflicts"]), rec
    assert "isbn" not in rec["_missing"], rec


def test_cover_candidates_ranked_apple_first():
    out = {
        "apple": [{"artworkUrl100": "https://x.mzstatic.com/img/100x100bb.jpg"}],
        "audible": [{"product_images": {"500": "https://m.media-amazon.com/img.jpg"}}],
        "openlibrary": [], "google": [],
    }
    cands = mf.cover_candidates(out)
    assert cands[0]["source"] == "apple", cands
    assert cands[0]["url"].endswith("2400x2400bb.jpg"), cands


def test_sanitize_filename():
    assert cover_embed.sanitize_filename("Project Hail Mary!") == "Project-Hail-Mary"


def test_build_exiftool_args_includes_comment_json_and_narrator():
    meta = {"title": "Project Hail Mary", "authors": ["Andy Weir"],
            "narrators": ["Ray Porter"], "length": "16h 10m", "average_rating": 4.49}
    args = cover_embed.build_exiftool_args(meta)
    joined = " ".join(args)
    assert "-XMP-dc:Title=Project Hail Mary" in args, args
    assert "-XMP-dc:Contributor=Ray Porter" in args, args
    assert "-XMP-xmp:Rating=4" in args, args  # rounds to nearest int
    comment_arg = next(a for a in args if a.startswith("-Comment="))
    payload = json.loads(comment_arg[len("-Comment="):])
    assert payload["narrators"] == ["Ray Porter"], payload  # only lives in Comment JSON
    assert "16h 10m" in joined


def test_embed_metadata_end_to_end_with_real_exiftool(tmp_path: Path):
    """Exercises the actual exiftool subprocess call (not just build_exiftool_args),
    against a real minimal JPEG, and reads the tags back."""
    if shutil.which("exiftool") is None:
        pytest.skip("exiftool not on PATH")

    # Smallest valid JPEG: 1x1 pixel, generated fresh rather than
    # hardcoding bytes that might not decode identically everywhere.
    img_path = tmp_path / "cover.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=8x8", "-frames:v", "1",
         "-loglevel", "error", str(img_path)],
        check=True,
    )

    meta = {"title": "Project Hail Mary", "authors": ["Andy Weir"], "narrators": ["Ray Porter"],
            "publisher": "Audible Studios", "published_date": "2021-05-04",
            "average_rating": 4.49, "length": "16h 10m",
            "source_url": "https://www.audible.com/pd/B08G9PRS1K"}
    result = cover_embed.embed_metadata(img_path, meta)
    assert result["dry_run"] is False
    assert "updated" in result["output"].lower(), result

    readback = subprocess.run(
        ["exiftool", "-s3", "-XMP-dc:Title", "-XMP-dc:Creator", "-XMP-dc:Publisher", "-Comment",
         str(img_path)],
        capture_output=True, text=True, check=True,
    )
    lines = readback.stdout.strip().splitlines()
    assert lines[0] == "Project Hail Mary", lines
    assert lines[1] == "Andy Weir", lines
    assert lines[2] == "Audible Studios", lines
    comment = json.loads(lines[3])
    assert comment["narrators"] == ["Ray Porter"], comment

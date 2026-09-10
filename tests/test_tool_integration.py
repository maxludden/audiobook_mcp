#!/usr/bin/env python3
"""
Exercises the actual audiobook_fetch_cover_image / audiobook_embed_cover_metadata
MCP tool functions (Pydantic validation -> async wrapper -> real file I/O),
using a file:// URL so no live internet is needed (urllib.request handles
file:// natively, so this is the real download_to_file() code path, not a
mock). audiobook_lookup_book_metadata itself needs live internet to the
four source APIs (blocked in this sandbox -- see test_metadata_fetch.py's
module docstring) and isn't exercised here.

Run:
    python3 tests/test_tool_integration.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audiobook_mcp import server, models as m  # noqa: E402


async def run(tmp_path: Path) -> None:
    # Build a real small JPEG to serve as the "cover" being fetched.
    src_img = tmp_path / "source.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=16x16", "-frames:v", "1",
         "-loglevel", "error", str(src_img)],
        check=True,
    )
    file_url = src_img.resolve().as_uri()

    out_dir = tmp_path / "covers"
    fetch_result = await server.audiobook_fetch_cover_image(m.FetchCoverImageInput(
        cover_url=file_url, out_dir=str(out_dir), title="Project Hail Mary!", author="Andy Weir",
    ))
    fetch_data = json.loads(fetch_result)
    print("fetch_cover_image ->", fetch_data)
    assert "error" not in fetch_data, fetch_data
    dest = Path(fetch_data["path"])
    assert dest.name == "Project-Hail-Mary_Andy-Weir.jpg", dest
    assert dest.exists(), dest
    assert fetch_data["width"] == 16 and fetch_data["height"] == 16, fetch_data

    # Re-fetching without overwrite=True must refuse, not clobber.
    refetch = json.loads(await server.audiobook_fetch_cover_image(m.FetchCoverImageInput(
        cover_url=file_url, out_dir=str(out_dir), title="Project Hail Mary!", author="Andy Weir",
    )))
    assert "error" in refetch, refetch
    print("duplicate fetch correctly refused ->", refetch["error"][:60], "...")

    if shutil.which("exiftool") is None:
        print("SKIP embed step: exiftool not on PATH")
        return

    embed_result = json.loads(await server.audiobook_embed_cover_metadata(m.EmbedCoverMetadataInput(
        image_path=str(dest),
        metadata=m.BookMetadataInput(
            title="Project Hail Mary", authors=["Andy Weir"], narrators=["Ray Porter"],
            publisher="Audible Studios", published_date="2021-05-04",
            average_rating=4.49, length="16h 10m",
        ),
    )))
    print("embed_cover_metadata ->", embed_result)
    assert "error" not in embed_result, embed_result
    assert embed_result["dry_run"] is False

    readback = subprocess.run(
        ["exiftool", "-s3", "-XMP-dc:Title", "-Comment", str(dest)],
        capture_output=True, text=True, check=True,
    )
    lines = readback.stdout.strip().splitlines()
    assert lines[0] == "Project Hail Mary", lines
    comment = json.loads(lines[1])
    assert comment["narrators"] == ["Ray Porter"], comment
    print("readback confirmed:", lines[0], comment["narrators"])

    print("\nALL TOOL-INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run(Path(td)))

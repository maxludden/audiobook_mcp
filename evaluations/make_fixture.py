#!/usr/bin/env python3
"""
Build a tiny synthetic "book" (EPUB + a single-file MP3 with real silence
gaps at each chapter transition) so the full pipeline can be exercised
end-to-end in seconds, without needing a real audiobook.

Usage:
    python3 make_fixture.py --out-dir /tmp/audiobook_fixture

Produces:
    <out-dir>/fixture_book.epub
    <out-dir>/fixture_book.mp3

The generated audio uses a distinct tone per chapter (so a human can also
sanity-check segment boundaries by ear) separated by clean silence gaps of
varying length, including one short "decoy" gap in chapter 3's transition
to make sure the pipeline's double-gap filtering is exercised.
"""
from __future__ import annotations

import argparse
import subprocess
import zipfile
from pathlib import Path

CHAPTERS = [
    ("1. The Beginning", 6.0, 220),
    ("2. A Complication", 5.0, 330),
    ("3. The Decoy Gap", 7.0, 440),
    ("4. Resolution", 4.0, 550),
]

NAV_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Nav</title></head>
<body>
<nav epub:type="toc">
<ol>
{items}
</ol>
</nav>
</body>
</html>
"""

CHAPTER_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>{body}</p>
</body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:fixture-book-0001</dc:identifier>
    <dc:title>The Fixture Chronicles</dc:title>
    <dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
    <dc:date>2024-01-01</dc:date>
    <meta name="cover" content="cover-image"/>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>
    <item id="cover-image" href="cover.png" media-type="image/png" properties="cover-image"/>
    {manifest_items}
  </manifest>
  <spine>
    {spine_items}
  </spine>
</package>
"""

CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

COVER_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "cover.png"


def build_epub(out_path: Path) -> None:
    nav_items = "\n".join(f'<li><a href="ch{i+1}.xhtml">{title}</a></li>'
                           for i, (title, _dur, _freq) in enumerate(CHAPTERS))
    manifest_items = "\n".join(
        f'<item id="ch{i+1}" href="ch{i+1}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(CHAPTERS))
    )
    spine_items = "\n".join(f'<itemref idref="ch{i+1}"/>' for i in range(len(CHAPTERS)))

    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", OPF.format(manifest_items=manifest_items,
                                                      spine_items=spine_items))
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML.format(items=nav_items))
        if COVER_IMAGE_PATH.exists():
            zf.write(COVER_IMAGE_PATH, "OEBPS/cover.png")
        else:
            raise FileNotFoundError(
                f"Expected a cover image at {COVER_IMAGE_PATH} (used to exercise cover-art "
                "detection/embedding in the smoke test)."
            )
        for i, (title, _dur, _freq) in enumerate(CHAPTERS):
            body = " ".join(["word"] * (30 * (i + 1)))  # distinct word counts per chapter
            zf.writestr(f"OEBPS/ch{i+1}.xhtml", CHAPTER_XHTML.format(title=title, body=body))


def build_audio(out_path: Path) -> None:
    """Concatenate: [tone][silence][tone][silence]... with a real ~4s gap
    plus a short ~2s decoy gap 4s later at the chapter-3 transition, to
    exercise the pipeline's double-gap filtering."""
    parts = []
    gap_after = [4.0, 4.5, [4.0, 4.0, 2.0], 3.5]  # ch3's gap is [real, speech, decoy]
    for i, (_title, dur, freq) in enumerate(CHAPTERS):
        parts.append(("tone", dur, freq))
        if i < len(CHAPTERS) - 1:
            gap = gap_after[i]
            if isinstance(gap, list):
                real_gap, mini_speech, decoy_gap = gap
                parts.append(("silence", real_gap, 0))
                parts.append(("tone", mini_speech, 600))  # brief announcement-like blip
                parts.append(("silence", decoy_gap, 0))
            else:
                parts.append(("silence", gap, 0))

    filter_inputs = []
    concat_inputs = []
    for idx, (kind, dur, freq) in enumerate(parts):
        label = f"[a{idx}]"
        if kind == "tone":
            filter_inputs.append(f"sine=frequency={freq}:duration={dur}{label}")
        else:
            filter_inputs.append(f"anullsrc=r=32000:cl=mono:duration={dur}{label}")
        concat_inputs.append(label)
    filter_complex = ";".join(filter_inputs) + ";" + "".join(concat_inputs) + \
        f"concat=n={len(parts)}:v=0:a=1[out]"

    cmd = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-filter_complex", filter_complex, "-map", "[out]",
        "-c:a", "libmp3lame", "-b:a", "64k", str(out_path),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    epub_path = args.out_dir / "fixture_book.epub"
    mp3_path = args.out_dir / "fixture_book.mp3"
    build_epub(epub_path)
    build_audio(mp3_path)
    print(f"wrote {epub_path}")
    print(f"wrote {mp3_path}")


if __name__ == "__main__":
    main()

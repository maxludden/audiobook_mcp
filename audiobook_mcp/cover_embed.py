"""
Embed normalized book metadata into a cover image's EXIF/XMP/IPTC via
exiftool, ported from the book-metadata-fetch skill
(third_party/book-metadata-fetch/scripts/embed_metadata.py).

`build_exiftool_args` is unchanged from the source skill. What changed:
no argparse/stdin CLI (this is called directly with a dict, like every
other tool in this server); exiftool's own availability is checked with
the same actionable-error pattern as ffmpeg in audio_probe.py, per the
skill's own guardrail ("don't silently skip metadata embedding -- tell
the user").
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


class ExiftoolNotFoundError(RuntimeError):
    pass


class MetadataEmbedError(RuntimeError):
    pass


def check_exiftool_available() -> None:
    if shutil.which("exiftool") is None:
        raise ExiftoolNotFoundError(
            "exiftool not found on PATH. Install it (e.g. `apt install libimage-exiftool-perl` "
            "on Debian/Ubuntu, `brew install exiftool` on macOS) and make sure the audiobook_mcp "
            "server process can see it on PATH, then retry. Metadata was NOT silently skipped -- "
            "nothing was written."
        )


def sanitize_filename(text: str) -> str:
    """'Project Hail Mary' -> 'Project-Hail-Mary' (spaces to hyphens, strip
    characters unsafe in filenames), matching the source skill's naming
    convention for downloaded covers."""
    text = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[\s]+", "-", text) or "untitled"


def build_exiftool_args(meta: dict) -> list[str]:
    """Build the -TAG=value argument list for exiftool from a reconciled
    metadata record (see metadata_fetch.reconcile's return shape).
    Unchanged from the source skill."""
    args: list[str] = []

    def add(tag, value):
        if value:
            args.append(f"-{tag}={value}")

    title = meta.get("title", "") or ""
    authors = meta.get("authors") or []
    author_str = ", ".join(authors)
    narrators = meta.get("narrators") or []
    narrator_str = ", ".join(narrators)
    description = meta.get("description") or ""

    add("XMP-dc:Title", title)
    add("IPTC:ObjectName", title[:64])
    add("XMP-dc:Creator", author_str)
    add("EXIF:Artist", author_str)
    if narrator_str:
        add("XMP-dc:Contributor", narrator_str)
    add("XMP-dc:Description", description[:2000])
    add("EXIF:ImageDescription", description[:200])
    add("XMP-dc:Publisher", meta.get("publisher", ""))
    add("IPTC:CopyrightNotice", meta.get("copyright", ""))
    add("EXIF:Copyright", meta.get("copyright", ""))

    pub_date = meta.get("published_date", "") or ""
    if pub_date:
        add("XMP-dc:Date", pub_date)
        add("IPTC:DateCreated", pub_date.replace("-", ""))

    categories = meta.get("categories") or []
    if categories:
        args.append("-XMP-dc:Subject=" + ",".join(categories))
        args.append("-IPTC:Keywords=" + ",".join(categories))

    add("XMP-dc:Source", meta.get("source_url", ""))

    rating = meta.get("average_rating")
    if rating is not None:
        try:
            add("XMP-xmp:Rating", str(round(float(rating))))
        except (TypeError, ValueError):
            pass

    length = meta.get("length")
    if length:
        add("XMP-dc:Format", str(length))

    # Full record preserved verbatim, even fields with no standard tag
    # (narrator and duration have no EXIF equivalent -- they live only
    # here, matching the source skill's design).
    args.append("-Comment=" + json.dumps(meta, ensure_ascii=False))
    return args


def embed_metadata(image_path: Path, meta: dict, dry_run: bool = False) -> dict:
    """Write `meta` into image_path's EXIF/XMP/IPTC tags via exiftool.
    Returns {"command": [...], "output": str} (output is the dry-run
    command string, or exiftool's own stdout, when not a dry run)."""
    if not image_path.exists():
        raise MetadataEmbedError(f"Image not found: {image_path}")
    check_exiftool_available()

    exif_args = build_exiftool_args(meta)
    cmd = ["exiftool", "-overwrite_original", *exif_args, str(image_path)]

    if dry_run:
        return {"command": cmd, "output": None, "dry_run": True}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise MetadataEmbedError(f"exiftool failed: {result.stderr.strip()[-500:]}")
    return {"command": cmd, "output": result.stdout.strip(), "dry_run": False}

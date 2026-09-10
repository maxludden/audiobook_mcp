#!/usr/bin/env python3
"""Embed normalized book metadata into a cover image's EXIF/XMP/IPTC via exiftool.

Requires exiftool on PATH (`brew install exiftool`).

Usage:
    embed_metadata.py cover.jpg metadata.json
    cat metadata.json | embed_metadata.py cover.jpg -

metadata.json shape (all fields optional):
{
  "title": "...",
  "authors": ["..."],
  "narrators": ["..."],
  "description": "...",
  "publisher": "...",
  "copyright": "...",
  "published_date": "YYYY-MM-DD",
  "categories": ["...", "..."],
  "source_url": "https://...",
  "average_rating": 4.5,
  "ratings_count": 1234,
  "length": "10h 32m"  // or page count, e.g. "412 pages"
}

The full metadata dict is also stamped into the JPEG Comment field as JSON so no
field is lost even where no standard EXIF/XMP/IPTC tag fits it.
"""
import argparse
import json
import subprocess
import sys


def build_args(meta: dict) -> list:
    args = []

    def add(tag, value):
        if value:
            args.append(f"-{tag}={value}")

    title = meta.get("title", "")
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

    pub_date = meta.get("published_date", "")
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

    # Full record preserved verbatim, even fields with no standard tag.
    args.append("-Comment=" + json.dumps(meta, ensure_ascii=False))
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", help="Path to the cover image file")
    parser.add_argument("metadata_json", help="Path to a JSON file, or '-' to read JSON from stdin")
    parser.add_argument("--dry-run", action="store_true", help="Print the exiftool command without writing")
    args = parser.parse_args()

    if args.metadata_json == "-":
        meta = json.load(sys.stdin)
    else:
        with open(args.metadata_json) as f:
            meta = json.load(f)

    exif_args = build_args(meta)
    cmd = ["exiftool", "-overwrite_original", *exif_args, args.image]

    if args.dry_run:
        print(" ".join(json.dumps(a) if " " in a else a for a in cmd))
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    print(result.stdout.strip() or f"Metadata written to {args.image}")


if __name__ == "__main__":
    main()

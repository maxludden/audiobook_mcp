"""
Generalized EPUB chapter/metadata extraction.

Works for any EPUB2 (toc.ncx) or EPUB3 (nav.xhtml) book, not just one
series. Chapter detection strategy:
  1. Preferred: TOC entries whose label matches "<number>. <title>" (the
     common pattern for numbered-chapter fiction).
  2. Fallback: every spine item NOT matching a front/back-matter title
     blocklist (title page, copyright, contents, acknowledgments, also-by,
     about the author, etc.), taken in spine order.

Ported from the audiobook-mp3-to-m4b skill's scripts/epub_extract.py, with
the CLI entry point removed (this module is used as a library inside the
MCP server) and errors raised as exceptions with actionable messages
instead of crashing with a bare traceback.
"""
from __future__ import annotations

import re
import warnings
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

FRONT_BACK_MATTER_BLOCKLIST = [
    "cover", "title page", "titlepage", "copyright", "also by", "contents",
    "table of contents", "dedication", "acknowledg", "about the author",
    "afterword", "epilogue note", "author's note", "author\u2019s note",
    "thank you for reading", "map", "glossary", "appendix", "preview",
    "sneak peek", "discord", "newsletter", "groups",
]

NUMBERED_RE = re.compile(r"^\s*(\d+)\s*[\.\):]\s*(.+?)\s*$")

_BLOCKLIST_RES = [re.compile(r"\b" + re.escape(b) + r"\b") for b in FRONT_BACK_MATTER_BLOCKLIST]


class EpubExtractError(ValueError):
    """The EPUB could not be parsed, or no chapters could be located."""


def _is_front_back_matter(heading: str) -> bool:
    h = heading.lower()
    return any(r.search(h) for r in _BLOCKLIST_RES)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_epub(epub_path: Path, work_dir: Path) -> dict:
    """Unzip `epub_path` into `work_dir` and return:

        {
          "meta": {"title", "author", "publisher", "year", "series", "series_index"},
          "cover": str | None,       # absolute path to the best cover image found
          "chapters": [
            {"n": int, "title": str, "word_count": int, "_href": str}, ...
          ],
        }

    Raises EpubExtractError if the file isn't a valid EPUB or no chapters
    could be located at all.
    """
    if not epub_path.exists():
        raise EpubExtractError(f"EPUB not found: {epub_path}")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(epub_path) as zf:
            zf.extractall(work_dir)
    except zipfile.BadZipFile as e:
        raise EpubExtractError(
            f"{epub_path} is not a valid EPUB (not a zip archive)."
        ) from e

    opf_path = _find_opf(work_dir)
    opf = _read(opf_path)
    base = opf_path.parent

    manifest = dict(re.findall(r'<item\s+[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf))
    if not manifest:
        # some publishers put href before id; catch that ordering too
        manifest = dict((m[1], m[0]) for m in
                         re.findall(r'<item\s+[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf))
    spine = re.findall(r'<itemref\s+idref="([^"]+)"', opf)

    meta = _extract_metadata(opf)
    cover_path = _find_cover(opf, manifest, base)

    chapters = _extract_chapters_via_toc(base, manifest, spine)
    if not chapters:
        chapters = _extract_chapters_via_blocklist(base, manifest, spine)
    if not chapters:
        raise EpubExtractError(
            f"Could not locate any chapters in {epub_path.name}. The EPUB may use an "
            "unusual structure (no TOC nav/ncx and no identifiable spine chapters). "
            "You can still proceed by supplying manual chapter data -- see the "
            "'Without an EPUB' section of the audiobook-mp3-to-m4b skill."
        )

    return {
        "meta": meta,
        "cover": str(cover_path) if cover_path else None,
        "chapters": chapters,
    }


def _find_opf(work_dir: Path) -> Path:
    container = work_dir / "META-INF" / "container.xml"
    if container.exists():
        m = re.search(r'full-path="([^"]+)"', _read(container))
        if m:
            candidate = work_dir / m.group(1)
            if candidate.exists():
                return candidate
    candidates = list(work_dir.rglob("*.opf"))
    if not candidates:
        raise EpubExtractError(
            "No .opf package file found -- this doesn't look like a valid EPUB."
        )
    return candidates[0]


def _extract_metadata(opf: str) -> dict:
    def find(pattern, default=""):
        m = re.search(pattern, opf, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else default

    title = find(r"<dc:title[^>]*>(.*?)</dc:title>")
    author = find(r"<dc:creator[^>]*>(.*?)</dc:creator>")
    publisher = find(r"<dc:publisher[^>]*>(.*?)</dc:publisher>")
    date = find(r"<dc:date[^>]*>(.*?)</dc:date>")
    year = date[:4] if date[:4].isdigit() else ""
    series_m = re.search(r'name="calibre:series"\s+content="([^"]*)"', opf)
    series_idx_m = re.search(r'name="calibre:series_index"\s+content="([^"]*)"', opf)
    return {
        "title": title or "Unknown Title",
        "author": author or "Unknown Author",
        "publisher": publisher,
        "year": year,
        "series": series_m.group(1) if series_m else "",
        "series_index": series_idx_m.group(1) if series_idx_m else "",
    }


def _find_cover(opf: str, manifest: dict, base: Path) -> Path | None:
    m = re.search(r'name="cover"\s+content="([^"]+)"', opf)
    if m and m.group(1) in manifest:
        p = base / manifest[m.group(1)]
        if p.exists():
            return p
    # fallback: manifest item whose id/href contains "cover" and is an image
    for item_id, href in manifest.items():
        if "cover" in item_id.lower() and re.search(r"\.(jpe?g|png)$", href, re.I):
            p = base / href
            if p.exists():
                return p
    return None


def _chapter_word_count(path: Path) -> tuple[str, int]:
    soup = BeautifulSoup(_read(path), "lxml")
    text = soup.get_text(" ", strip=True)
    heading = ""
    for tag in ["h1", "h2", "h3"]:
        h = soup.find(tag)
        if h:
            heading = h.get_text(" ", strip=True)
            break
    return heading, len(text.split())


def _extract_chapters_via_toc(base: Path, manifest: dict, spine: list[str]) -> list[dict]:
    toc_ncx = list(base.rglob("*.ncx"))
    nav_html = list(base.rglob("nav.xhtml"))
    entries: list[tuple[str, str]] = []  # (label, href)

    if nav_html:
        soup = BeautifulSoup(_read(nav_html[0]), "lxml")
        toc_nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
        if toc_nav:
            for a in toc_nav.find_all("a", href=True):
                entries.append((a.get_text(" ", strip=True), a["href"].split("#")[0]))
    elif toc_ncx:
        ncx = _read(toc_ncx[0])
        for nav_point in re.finditer(
                r"<navPoint[^>]*>.*?<text>([^<]*)</text>.*?src=\"([^\"]+)\"", ncx, re.S):
            entries.append((nav_point.group(1).strip(), nav_point.group(2).split("#")[0]))

    # spine gives reading order; a chapter's real text often spans multiple
    # spine files when the source HTML was auto-split (part0005_split_000,
    # _001, _002, ...). Resolve each TOC entry to its spine index, then sum
    # word counts across every spine file up to (not including) the next
    # chapter's spine index.
    spine_hrefs = [manifest[idref] for idref in spine if idref in manifest]
    href_to_index = {href.split("/")[-1]: i for i, href in enumerate(spine_hrefs)}

    numbered = []  # (n, title, spine_index)
    for label, href in entries:
        m = NUMBERED_RE.match(label)
        if not m:
            continue
        idx = href_to_index.get(href.split("/")[-1])
        if idx is None:
            continue
        numbered.append((int(m.group(1)), m.group(2), idx))

    numbered.sort(key=lambda t: t[2])  # by spine position, not claimed number
    seen_n = set()
    deduped = []
    for n, title, idx in numbered:
        if n in seen_n:
            continue
        seen_n.add(n)
        deduped.append((n, title, idx))

    chapters = []
    for i, (n, title, start_idx) in enumerate(deduped):
        end_idx = deduped[i + 1][2] if i + 1 < len(deduped) else len(spine_hrefs)
        wc = 0
        for j, href in enumerate(spine_hrefs[start_idx:end_idx]):
            p = base / href
            if not p.exists():
                continue
            heading, w = _chapter_word_count(p)
            # guard against the last chapter silently absorbing trailing
            # back matter (thank-you page, afterword, ...) that has no
            # numbered TOC entry of its own to bound it
            if j > 0 and heading and _is_front_back_matter(heading):
                break
            wc += w
        chapters.append({"n": n, "title": title, "word_count": wc,
                          "_href": spine_hrefs[start_idx]})

    chapters.sort(key=lambda c: c["n"])
    return chapters


def _extract_chapters_via_blocklist(base: Path, manifest: dict, spine: list[str]) -> list[dict]:
    chapters = []
    n = 0
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        p = base / href
        if not p.exists() or p.name == "titlepage.xhtml":
            continue
        heading, wc = _chapter_word_count(p)
        label = (heading or "").lower()
        if _is_front_back_matter(label):
            continue
        if wc < 200:  # too short to plausibly be real chapter content
            continue
        n += 1
        chapters.append({"n": n, "title": heading or f"Chapter {n}", "word_count": wc, "_href": href})
    return chapters

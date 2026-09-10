#!/usr/bin/env python3
"""Query free, no-auth book/audiobook metadata APIs and print normalized JSON.

Sources:
  - Google Books API     (title, authors, publisher, date, description, ISBN, rating, thumbnail)
  - Open Library API     (title, authors, publisher, date, subjects, cover ids)
  - Audible catalog API  (api.audible.com) — the same public, unauthenticated JSON
    endpoint audible.com's own search box calls client-side. Best source for
    audiobook-specific fields: narrator(s), runtime, publisher, cover image, ASIN.
  - Apple iTunes Search API (itunes.apple.com) — public, unauthenticated. Its cover
    art (`artworkUrl100`) lives on the mzstatic CDN and can be upsized by replacing
    the trailing `100x100bb.jpg` with e.g. `2400x2400bb.jpg` — this CDN has been
    observed serving genuinely distinct, larger pixel data up to ~2400x2400 for some
    titles (unlike Amazon's CDN, which often just clamps to a small cached master).
    Usually the single best source of real HD cover art. See upsize_apple_artwork().

None of these require login, and none render/scrape an amazon.com or audible.com HTML
page — they're plain JSON API calls, so no CAPTCHA/bot-check surface is touched. Keep
usage to a handful of lookups per session; these are still free services, not a bulk
data feed.

Google Books' anonymous quota can be very tight (sometimes 0/day) depending on the
network's shared IP. If you hit HTTP 429 repeatedly, set GOOGLE_BOOKS_API_KEY (a free
key from https://console.cloud.google.com/apis/library/books.googleapis.com) and it
will be used automatically.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "book-metadata-fetch-skill/1.0 (personal research tool)"


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def query_google_books(title, author=None):
    q = f"intitle:{title}"
    if author:
        q += f"+inauthor:{author}"
    params = {"q": q, "maxResults": 20}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if api_key:
        params["key"] = api_key
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(
        params
    )
    data = _get_json(url)
    return data.get("items", [])


OL_FIELDS = (
    "title,author_name,first_publish_year,publisher,isbn,"
    "number_of_pages_median,subject,ratings_average,ratings_count,cover_i,key"
)


def query_open_library(title, author=None):
    # Explicit `fields=` is important: the default response is a huge blob that
    # omits ratings_average/ratings_count/number_of_pages_median entirely.
    params = {"title": title, "limit": 20, "fields": OL_FIELDS}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return data.get("docs", [])


def query_audible(title, author=None):
    keywords = title if not author else f"{title} {author}"
    params = {
        "keywords": keywords,
        "num_results": 20,
        "response_groups": "product_desc,product_attrs,contributors,media",
    }
    url = "https://api.audible.com/1.0/catalog/products?" + urllib.parse.urlencode(
        params
    )
    data = _get_json(url)
    return data.get("products", [])


def query_apple(title, author=None, entity="audiobook"):
    term = title if not author else f"{title} {author}"
    params = {"term": term, "entity": entity, "limit": 25}
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    return data.get("results", [])


def upsize_apple_artwork(artwork_url, size=2400):
    """Swap the '{n}x{n}bb.jpg' suffix on an mzstatic artwork URL for a larger size.

    The CDN silently clamps to whatever master it actually has (verify the
    downloaded file's real pixel dimensions), but has been observed serving
    genuinely larger, distinct images up to ~2400x2400 for many titles.
    """
    return re.sub(r"\d+x\d+bb\.jpg$", f"{size}x{size}bb.jpg", artwork_url)


SOURCES = {
    "google": query_google_books,
    "openlibrary": query_open_library,
    "audible": query_audible,
    "apple": query_apple,
}


# --- accuracy helpers -------------------------------------------------------


def _norm(s):
    """Lowercase, strip punctuation/extra whitespace — for loose title matching."""
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _volume_of(title):
    """Extract a trailing volume number from a series title, if present.

    'Defiance of the Fall 12 (Unabridged)' -> 12 ; 'Project Hail Mary' -> None
    """
    m = re.search(
        r"\b(\d{1,3})\b(?!.*\b\d{1,3}\b)", re.sub(r"\(.*?\)", "", title or "")
    )
    return int(m.group(1)) if m else None


def pick_best(results, title, key_title, key_date=None):
    """Choose the entry whose title best matches the query.

    Search APIs rank by relevance, NOT by series volume — an unfiltered [0] can
    hand you book 12 when you asked for book 1. Prefer an exact normalized title
    match, then a matching trailing volume number, then fall back to [0].
    """
    if not results:
        return None
    want = _norm(title)
    want_vol = _volume_of(title)

    exact = [r for r in results if _norm(key_title(r)) == want]
    if exact:
        results = exact
    elif want_vol is not None:
        same_vol = [r for r in results if _volume_of(key_title(r)) == want_vol]
        # Volume 1 of a series is usually published WITHOUT a number
        # ("Defiance of the Fall", not "... 1"), so an unnumbered title that
        # otherwise starts with the series name counts as volume 1.
        if not same_vol and want_vol == 1:
            base = _norm(re.sub(r"\s*\d+\s*$", "", title))
            same_vol = [
                r
                for r in results
                if _volume_of(key_title(r)) is None
                and _norm(key_title(r)).startswith(base)
            ]
        if same_vol:
            results = same_vol
        else:
            # The requested volume is simply not in the result set. Returning
            # results[0] here would hand back a DIFFERENT BOOK that looks
            # plausible (usually volume 1). Refuse instead.
            return None

    if key_date:
        # Drop far-future entries (unreleased re-issues) when anything else exists.
        dated = [r for r in results if (key_date(r) or "")[:4] <= "2026"]
        if dated:
            results = dated
    return results[0]


def probe_image(url, timeout=20):
    """Download an image URL and report its TRUE pixel dimensions.

    Never trust the URL or filename: Amazon's `_SL2400_` token is silently
    ignored (still returns the 500px master), while Apple's mzstatic really
    does serve up to 2400x2400. Only a real decode tells you which you got.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
    except Exception as e:
        return {"url": url, "error": str(e)}

    w = h = None
    # Minimal JPEG/PNG SOF parser — avoids a hard Pillow dependency.
    if blob[:2] == b"\xff\xd8":
        i = 2
        while i < len(blob) - 9:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker in (
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                h = int.from_bytes(blob[i + 5 : i + 7], "big")
                w = int.from_bytes(blob[i + 7 : i + 9], "big")
                break
            i += 2 + int.from_bytes(blob[i + 2 : i + 4], "big")
    elif blob[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(blob[16:20], "big")
        h = int.from_bytes(blob[20:24], "big")

    return {"url": url, "width": w, "height": h, "bytes": len(blob)}


def cover_candidates(out):
    """Ranked cover URLs, best-first, based on what actually serves HD."""
    cands = []
    for r in out.get("apple") or []:
        art = r.get("artworkUrl100")
        if art:
            cands.append(("apple", upsize_apple_artwork(art, 2400)))
    for r in out.get("audible") or []:
        img = (r.get("product_images") or {}).get("500")
        if img:
            cands.append(("audible", img))
    for r in out.get("openlibrary") or []:
        cid = r.get("cover_i")
        if cid:
            cands.append((
                "openlibrary",
                f"https://covers.openlibrary.org/b/id/{cid}-L.jpg",
            ))
    for r in out.get("google") or []:
        links = (r.get("volumeInfo") or {}).get("imageLinks") or {}
        for k in ("extraLarge", "large", "medium", "thumbnail"):
            if links.get(k):
                cands.append(("google", links[k].replace("http://", "https://")))
                break
    seen, uniq = set(), []
    for src, u in cands:
        if u not in seen:
            seen.add(u)
            uniq.append({"source": src, "url": u})
    return uniq


def reconcile(out, title):
    """Merge raw source payloads into one normalized record + provenance."""
    rec = {
        "title": None,
        "authors": [],
        "narrators": [],
        "description": None,
        "publisher": None,
        "published_date": None,
        "categories": [],
        "average_rating": None,
        "ratings_count": None,
        "length": None,
        "isbn": None,
        "source_url": None,
    }
    prov, conflicts = {}, []

    def put(field, value, src):
        if value in (None, "", [], {}):
            return
        if rec.get(field) in (None, "", [], {}):
            rec[field] = value
            prov[field] = src
        elif field in ("published_date", "publisher") and rec[field] != value:
            conflicts.append(
                f"{field}: {prov.get(field)}={rec[field]!r} vs {src}={value!r}"
            )

    aud = pick_best(
        out.get("audible") or [],
        title,
        lambda r: r.get("title", ""),
        lambda r: r.get("release_date", ""),
    )
    if aud:
        put("title", aud.get("title"), "audible")
        put("authors", [a["name"] for a in aud.get("authors") or []], "audible")
        put("narrators", [n["name"] for n in aud.get("narrators") or []], "audible")
        put("publisher", aud.get("publisher_name"), "audible")
        put("published_date", aud.get("release_date"), "audible")
        desc = re.sub(r"<[^>]+>", "", aud.get("merchandising_summary") or "").strip()
        put("description", desc, "audible")
        mins = aud.get("runtime_length_min")
        if mins:
            put("length", f"{mins // 60}h {mins % 60}m", "audible")
        if aud.get("asin"):
            put("source_url", f"https://www.audible.com/pd/{aud['asin']}", "audible")

    ol = pick_best(out.get("openlibrary") or [], title, lambda r: r.get("title", ""))
    if ol:
        put("title", ol.get("title"), "openlibrary")
        put("authors", ol.get("author_name") or [], "openlibrary")
        pubs = ol.get("publisher") or []
        put("publisher", pubs[0] if pubs else None, "openlibrary")
        yr = ol.get("first_publish_year")
        put("published_date", str(yr) if yr else None, "openlibrary")
        put("categories", (ol.get("subject") or [])[:8], "openlibrary")
        put("average_rating", ol.get("ratings_average"), "openlibrary")
        put("ratings_count", ol.get("ratings_count"), "openlibrary")
        pages = ol.get("number_of_pages_median")
        put("length", f"{pages} pages" if pages else None, "openlibrary")
        isbns = ol.get("isbn") or []
        put("isbn", isbns[0] if isbns else None, "openlibrary")

    for g in out.get("google") or []:
        vi = g.get("volumeInfo") or {}
        if _norm(vi.get("title", "")) != _norm(title):
            continue
        put("title", vi.get("title"), "google")
        put("authors", vi.get("authors") or [], "google")
        put("description", vi.get("description"), "google")
        put("publisher", vi.get("publisher"), "google")
        put("published_date", vi.get("publishedDate"), "google")
        put("categories", vi.get("categories") or [], "google")
        put("average_rating", vi.get("averageRating"), "google")
        put("ratings_count", vi.get("ratingsCount"), "google")
        break

    rec["_provenance"] = prov
    rec["_conflicts"] = conflicts
    rec["_missing"] = [
        k for k, v in rec.items() if not k.startswith("_") and v in (None, "", [])
    ]
    return rec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("--author", default=None)
    parser.add_argument("--source", choices=[*SOURCES.keys(), "all"], default="all")
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Merge all sources into ONE normalized record with provenance, "
        "conflicts and missing-field list (instead of raw payloads).",
    )
    parser.add_argument(
        "--covers",
        action="store_true",
        help="Also probe candidate cover URLs and report TRUE pixel dimensions.",
    )
    args = parser.parse_args()

    out = {}
    sources = SOURCES.keys() if args.source == "all" else [args.source]
    for name in sources:
        try:
            out[name] = SOURCES[name](args.title, args.author)
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}: {e.reason}"
            if name == "google" and e.code == 429:
                msg += (
                    " — Google Books' *shared* anonymous quota is exhausted"
                    " (global, not per-user; retrying or changing User-Agent"
                    " will not help). Set GOOGLE_BOOKS_API_KEY to fix."
                )
            out[f"{name}_error"] = msg
        except Exception as e:
            out[f"{name}_error"] = str(e)

    if args.reconcile:
        result = {
            "record": reconcile(out, args.title),
            "errors": {k: v for k, v in out.items() if k.endswith("_error")},
        }
        if args.covers:
            result["covers"] = [
                probe_image(c["url"]) | {"source": c["source"]}
                for c in cover_candidates(out)
            ]
    else:
        result = out
        if args.covers:
            result["covers"] = [
                probe_image(c["url"]) | {"source": c["source"]}
                for c in cover_candidates(out)
            ]

    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()

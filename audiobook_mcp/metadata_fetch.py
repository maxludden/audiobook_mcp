"""
Book/audiobook metadata lookup, ported from the book-metadata-fetch skill
(third_party/book-metadata-fetch/) into library code the MCP server can
call directly instead of shelling out to a CLI script.

Sources, all free/unauthenticated JSON APIs -- no HTML scraping, no
CAPTCHA/bot-check surface touched (see third_party/book-metadata-fetch/
references/sources.md for full details and the original skill's Tier-2
retail-browser fallback, which is a Claude Browser-tool capability and
deliberately NOT reimplemented here -- this server has no browser):

  - Google Books API      -- title, authors, publisher, date, description,
                              ISBN, rating, thumbnail.
  - Open Library API       -- title, authors, publisher, date, subjects,
                              ratings, cover ids.
  - Audible catalog API    -- api.audible.com/1.0/catalog/products, the
                              same public JSON endpoint audible.com's own
                              search box calls client-side. Best source
                              for narrator(s), runtime, and an audiobook
                              cover.
  - Apple iTunes Search API -- usually the best HD cover art source; its
                              mzstatic CDN genuinely re-renders larger
                              sizes rather than clamping to a small master.

Ported almost verbatim -- the reconciliation/ranking logic (pick_best,
reconcile, cover_candidates, probe_image's stdlib-only JPEG/PNG header
parsing) is unchanged. What changed: no argparse/stdout (this returns
values, like every other module in this package, for the same stdio-safety
reason as pipeline.py), and network calls run through a shared urllib
helper with clearer exceptions.

Guardrails carried over from the skill (see its SKILL.md "Guardrails"
section) -- these still apply to every caller of this module:
  - Personal research/cataloging use, not bulk redistribution.
  - Keep usage to on-demand single-title lookups, not a scraping loop --
    lookup_book_metadata() below self-throttles to help with this (see
    MIN_INTERVAL_SECONDS).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

USER_AGENT = "audiobook-mcp/0.1 (personal research tool; https://github.com/)"
MIN_INTERVAL_SECONDS = 1.5  # soft per-process throttle across all lookup calls

_throttle_lock = threading.Lock()
_last_call_at = 0.0


class MetadataLookupError(RuntimeError):
    """A metadata source request failed outright (network/timeout/parse)."""


def _throttle() -> None:
    """Block briefly if the previous lookup was very recent, so an eager
    caller looping over many titles can't accidentally hammer these free,
    shared-quota APIs. Not a hard rate limiter -- just a courteous floor."""
    global _last_call_at
    with _throttle_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise MetadataLookupError(f"Request to {url.split('?')[0]} failed: {e}") from e
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        # A source occasionally answers 200 with something that isn't
        # valid JSON (an HTML error/maintenance page, truncated body,
        # unexpected encoding). Wrap it the same way as a network failure
        # so every caller of this helper -- including _query_one_source's
        # "never raises" contract -- only has one exception type to catch.
        raise MetadataLookupError(f"Request to {url.split('?')[0]} returned unparseable data: {e}") from e


def query_google_books(title: str, author: str | None, api_key: str | None) -> list:
    q = f"intitle:{title}"
    if author:
        q += f"+inauthor:{author}"
    params = {"q": q, "maxResults": 20}
    if api_key:
        params["key"] = api_key
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)
    return _get_json(url).get("items", [])


OL_FIELDS = (
    "title,author_name,first_publish_year,publisher,isbn,"
    "number_of_pages_median,subject,ratings_average,ratings_count,cover_i,key"
)


def query_open_library(title: str, author: str | None) -> list:
    # Explicit `fields=` matters: the default response omits
    # ratings_average/ratings_count/number_of_pages_median entirely.
    params = {"title": title, "limit": 20, "fields": OL_FIELDS}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    return _get_json(url).get("docs", [])


def query_audible(title: str, author: str | None) -> list:
    keywords = title if not author else f"{title} {author}"
    params = {
        "keywords": keywords, "num_results": 20,
        "response_groups": "product_desc,product_attrs,contributors,media",
    }
    url = "https://api.audible.com/1.0/catalog/products?" + urllib.parse.urlencode(params)
    return _get_json(url).get("products", [])


def query_apple(title: str, author: str | None, entity: str = "audiobook") -> list:
    term = title if not author else f"{title} {author}"
    params = {"term": term, "entity": entity, "limit": 25}
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(params)
    return _get_json(url).get("results", [])


SOURCES: dict[str, Callable] = {
    "google": lambda title, author, google_api_key=None: query_google_books(title, author, google_api_key),
    "openlibrary": lambda title, author, **_: query_open_library(title, author),
    "audible": lambda title, author, **_: query_audible(title, author),
    "apple": lambda title, author, **_: query_apple(title, author),
}


def upsize_apple_artwork(artwork_url: str, size: int = 2400) -> str:
    """Swap the trailing '{n}x{n}bb.jpg' suffix on an mzstatic artwork URL
    for a larger size. The CDN clamps to whatever master it actually has
    (always verify real downloaded pixel dimensions -- see probe_image),
    but genuinely serves distinct, larger pixel data up to ~2400x2400 for
    many titles, unlike Amazon's CDN which often just clamps."""
    return re.sub(r"\d+x\d+bb\.jpg$", f"{size}x{size}bb.jpg", artwork_url)


# --------------------------------------------------------------- matching


def _norm(s: str | None) -> str:
    """Lowercase, strip punctuation/extra whitespace -- for loose title matching."""
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _volume_of(title: str | None) -> int | None:
    """Extract a trailing volume number from a series title, if present.
    'Defiance of the Fall 12 (Unabridged)' -> 12 ; 'Project Hail Mary' -> None
    """
    m = re.search(r"\b(\d{1,3})\b(?!.*\b\d{1,3}\b)", re.sub(r"\(.*?\)", "", title or ""))
    return int(m.group(1)) if m else None


def pick_best(results: list, title: str, key_title: Callable, key_date: Callable | None = None,
              current_year: int = 2026) -> dict | None:
    """Choose the entry whose title best matches the query.

    Search APIs rank by relevance, NOT by series volume -- an unfiltered
    [0] can hand back book 12 when book 1 was asked for. Prefer an exact
    normalized title match, then a matching trailing volume number
    (refusing to guess if that volume genuinely isn't in the result set),
    then drop far-future entries (unreleased re-issues) when alternatives
    exist, then fall back to the first remaining result.
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
        # ("Defiance of the Fall", not "... 1"), so an unnumbered title
        # that otherwise starts with the series name counts as volume 1.
        if not same_vol and want_vol == 1:
            base = _norm(re.sub(r"\s*\d+\s*$", "", title))
            same_vol = [r for r in results if _volume_of(key_title(r)) is None
                        and _norm(key_title(r)).startswith(base)]
        if same_vol:
            results = same_vol
        else:
            # The requested volume isn't in the result set. Returning
            # results[0] here would hand back a DIFFERENT BOOK that looks
            # plausible (usually volume 1). Refuse instead.
            return None

    if key_date:
        dated = [r for r in results if (key_date(r) or "")[:4] <= str(current_year)]
        if dated:
            results = dated
    return results[0]


def probe_image(url: str, timeout: float = 20.0) -> dict:
    """Download an image URL and report its TRUE decoded pixel dimensions.

    Never trust the URL or filename: some CDNs silently clamp a resize
    token to a small cached master. Uses a minimal stdlib-only JPEG/PNG
    header parser (no Pillow dependency, matching the source skill).
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
    except urllib.error.URLError as e:
        return {"url": url, "error": str(e)}

    w, h = _decode_dimensions(blob)
    return {"url": url, "width": w, "height": h, "bytes": len(blob), "_data": blob}


def _decode_dimensions(blob: bytes) -> tuple[int | None, int | None]:
    w = h = None
    if blob[:2] == b"\xff\xd8":
        i = 2
        while i < len(blob) - 9:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h = int.from_bytes(blob[i + 5:i + 7], "big")
                w = int.from_bytes(blob[i + 7:i + 9], "big")
                break
            i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
    elif blob[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(blob[16:20], "big")
        h = int.from_bytes(blob[20:24], "big")
    return w, h


def download_to_file(url: str, dest_path, timeout: float = 30.0) -> dict:
    """Download `url` to `dest_path` and report its true decoded pixel
    dimensions (same header parser as probe_image, applied to the bytes
    actually written to disk -- one fetch, not two)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            blob = resp.read()
    except urllib.error.URLError as e:
        raise MetadataLookupError(f"Failed to download {url}: {e}") from e
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(blob)
    w, h = _decode_dimensions(blob)
    return {"path": str(dest_path), "width": w, "height": h, "bytes": len(blob)}


def cover_candidates(out: dict) -> list[dict]:
    """Ranked cover URLs, best-first, based on what actually tends to serve HD."""
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
            cands.append(("openlibrary", f"https://covers.openlibrary.org/b/id/{cid}-L.jpg"))
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


def reconcile(out: dict, title: str) -> dict:
    """Merge raw per-source payloads into one normalized record + provenance."""
    rec = {
        "title": None, "authors": [], "narrators": [], "description": None,
        "publisher": None, "published_date": None, "categories": [],
        "average_rating": None, "ratings_count": None, "length": None,
        "isbn": None, "source_url": None,
    }
    prov: dict = {}
    conflicts: list = []

    def put(field, value, src):
        if value in (None, "", [], {}):
            return
        if rec.get(field) in (None, "", [], {}):
            rec[field] = value
            prov[field] = src
        elif field in ("published_date", "publisher") and rec[field] != value:
            conflicts.append(f"{field}: {prov.get(field)}={rec[field]!r} vs {src}={value!r}")

    aud = pick_best(out.get("audible") or [], title, lambda r: r.get("title", ""),
                     lambda r: r.get("release_date", ""))
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
    rec["_missing"] = [k for k, v in rec.items() if not k.startswith("_") and v in (None, "", [])]
    return rec


# ------------------------------------------------------------- entry point


def _query_one_source(name: str, title: str, author: str | None,
                       google_api_key: str | None) -> tuple[str, list | None, str | None]:
    """Run one named source's query and return (name, results, error) --
    never raises, so it's safe to fan out across a thread pool without one
    source's failure cancelling the others."""
    try:
        return name, SOURCES[name](title, author, google_api_key=google_api_key), None
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.reason}"
        if name == "google" and e.code == 429:
            msg += (" -- Google Books' shared anonymous quota is exhausted globally, "
                    "not per-caller; retrying will not help. Set a Google Books API key "
                    "to fix this (see docstring in metadata_fetch.py).")
        return name, None, msg
    except MetadataLookupError as e:
        return name, None, str(e)


def lookup_book_metadata(title: str, author: str | None, sources: list[str],
                          reconcile_results: bool, include_covers: bool,
                          google_api_key: str | None) -> dict:
    """Query the requested sources, optionally reconcile into one record
    and/or probe cover candidates for real pixel dimensions. This is the
    one function server.py's tool calls -- everything above is a helper.

    The requested sources (and, below, cover-image probes) are independent
    network calls with no data dependency on each other, so they're fanned
    out across a thread pool instead of run one-by-one -- one lookup's
    total latency is then whichever single source/cover is slowest, not
    their sum. `_throttle()` still runs exactly once per call regardless,
    so this doesn't change how often the *shared* per-process rate limit
    lets a caller reach these APIs -- only how much wall-clock time one
    already-permitted call takes.
    """
    _throttle()
    out: dict = {}
    if sources:
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            for name, data, err in pool.map(
                    lambda n: _query_one_source(n, title, author, google_api_key), sources):
                if err is not None:
                    out[f"{name}_error"] = err
                else:
                    out[name] = data

    result: dict = {}
    if reconcile_results:
        result["record"] = reconcile(out, title)
    else:
        result["raw"] = {k: v for k, v in out.items() if not k.endswith("_error")}
    result["errors"] = {k: v for k, v in out.items() if k.endswith("_error")}

    if include_covers:
        candidates = cover_candidates(out)
        covers = []
        if candidates:
            with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
                probed_list = pool.map(lambda c: probe_image(c["url"]), candidates)
                for candidate, probed in zip(candidates, probed_list):
                    probed.pop("_data", None)
                    covers.append({**probed, "source": candidate["source"]})
        result["covers"] = covers
    return result

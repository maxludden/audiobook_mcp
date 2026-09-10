---
name: book-metadata-fetch
description: Look up book/audiobook metadata (author, narrator, publisher, publish date, copyright, description, ratings, page count/duration, categories) and download an HD cover image with that metadata embedded as EXIF/XMP/IPTC. Use this whenever the user asks to "get the cover art for X", "find the cover of X by Y", "look up metadata for this book/audiobook", "who narrates X", "download the cover for X", or gives any book/audiobook title (with or without an author) and wants its cover, publisher info, ratings, or narrator pulled down. Always trigger on requests shaped like "cover art of <book> by <author>" even if the title/author look like placeholders.
---

# Book metadata fetch

Fetches book/audiobook metadata and cover art using free, no-login APIs first, with a
narrowly-scoped Amazon/Audible browser fallback only when those APIs come up short. Amazon
and Audible have aggressive bot detection and scraping them in bulk violates their ToS —
this skill is built to stay on the safe side of that by default. See
[references/sources.md](references/sources.md) for full API details and the Amazon
fallback procedure.

## Requirements

- `exiftool` on PATH (`brew install exiftool`). If missing, ask the user before installing
  anything, or tell them to run the brew command themselves. Verified with exiftool 13.55.
- `python3` — stdlib only, no pip installs needed. Verified with Python 3.14.6.
- `sips` (built into macOS) for verifying real pixel dimensions.
- Default output folder: `~/Documents/BookMetadata/` (create if missing). Ask the user if
  they want a different destination for this request.
- Optional: `GOOGLE_BOOKS_API_KEY` — without it Google Books always 429s (see Gotchas).

## Workflow

1. **Identify the book.** Extract title (required) and author (optional but helps
   disambiguate). If the user's request is genuinely too vague to search — no title at all —
   ask a clarifying question before proceeding.

2. **Query and reconcile in one step** — this is the primary path:
   ```
   python3 scripts/lookup_book.py "<title>" --author "<author>" --reconcile --covers
   ```
   This queries Google Books, Open Library, Audible's public catalog API
   (`api.audible.com` — the same unauthenticated JSON endpoint audible.com's own search
   box calls; not a scrape) and the iTunes Search API, then merges them into **one
   normalized record** plus:
   - `_provenance` — which source supplied each field
   - `_conflicts` — fields where sources materially disagree
   - `_missing` — fields nothing supplied
   - `covers` — candidate cover URLs with their **true decoded pixel dimensions**

   Drop `--reconcile` to inspect the raw per-source payloads instead; add
   `--source audible` (or `google`/`openlibrary`/`apple`) to query just one.

   Verified output shape for `"Project Hail Mary" --author "Andy Weir"`:
   ```json
   { "record": { "title": "Project Hail Mary", "authors": ["Andy Weir"],
       "narrators": ["Ray Porter"], "publisher": "Audible Studios",
       "published_date": "2021-05-04", "length": "16h 10m",
       "average_rating": 4.491228, "isbn": "0593135229",
       "_conflicts": ["publisher: audible='Audible Studios' vs openlibrary='Penguin Random House'"] } }
   ```

3. **Resolve the `_conflicts` before writing anything.** They are usually *real* and
   meaningful, not noise — the most common one is audiobook publisher/date vs print
   publisher/date. Decide which edition the user actually asked for and say so in your
   reply; don't silently take whichever source won the merge.

4. **Pick the cover from the `covers` array by measured `width`/`height`**, not by
   source order or URL. Take the highest-resolution candidate that is the right edition
   (≥ ~1000px on the long edge). Ranking is empirically: Apple/mzstatic (2400×2400) ≫
   Audible (500×500) > Open Library `-L` (325×500). If nothing clears the bar, fall back
   to the **Tier 2 retail browser lookup** in references/sources.md — frequently
   unavailable since the Browser pane blocks most major retail domains by policy; one
   page visit for this one title if it does work, never a bulk crawl, and never attempt
   to bypass a CAPTCHA or sign-in wall (stop and tell the user if one appears).

5. **Download the image** to the destination folder as
   `<Sanitized-Title>_<Sanitized-Primary-Author>.jpg` (spaces → hyphens, strip punctuation
   unsafe for filenames). If a file with that name already exists, ask before overwriting.
   ```
   curl -sL -o "Project-Hail-Mary_Andy-Weir.jpg" "<chosen-cover-url>"
   sips -g pixelWidth -g pixelHeight "Project-Hail-Mary_Andy-Weir.jpg"   # confirm it's really HD
   ```

6. **Embed metadata** into the downloaded image. Both forms work:
   ```
   python3 scripts/embed_metadata.py "<path-to-image>" metadata.json
   ```
   ```
   python3 scripts/embed_metadata.py "<path-to-image>" - <<'EOF'
   {"title":"...","authors":["..."],"narrators":["..."],"length":"16h 10m"}
   EOF
   ```
   Prints `1 image files updated` on success. This writes standard EXIF/XMP/IPTC fields
   (title, creator, description, publisher, copyright, date, keywords, source URL, rating)
   and also stamps the complete record into the JPEG Comment field as JSON, so nothing
   gets lost where no standard tag fits (narrator and duration have no EXIF equivalent —
   they live *only* in the Comment JSON).

7. **Verify the readback** before reporting success:
   ```
   exiftool -Title -Creator -Publisher -DateCreated -ImageSize "<path-to-image>"
   exiftool -s3 -Comment "<path-to-image>" | python3 -m json.tool
   ```

8. **Report back**: file path, image dimensions, the key fields found, which source
   supplied each (`_provenance`), and anything in `_conflicts` / `_missing`.

## Gotchas (all verified against live APIs)

- **Never take `results[0]`.** These APIs rank by relevance, and the ranking is unstable
  across queries. Searching Apple for `"Defiance of the Fall"` with
  `--author "TheFirstDefier"` returns **book 12** at `[0]`; the same title *without* the
  author returns book 1. `--reconcile` guards against this by preferring an exact title
  match, then a matching trailing volume number. Series volumes are where wrong metadata
  actually comes from.
- **Google Books returns HTTP 429 for anonymous callers.** The quota is on Google's
  *shared* anonymous project (`project_number:624717413613`), so it is exhausted
  globally — not by you. Retrying, changing User-Agent, or adding `&country=US` does
  **not** help; only setting `GOOGLE_BOOKS_API_KEY` does. Treat Google as optional: the
  other three sources cover every field it would have supplied except a long-form
  description. As of this writing it 429s on every call.
- **Amazon's `_SL<n>_` resize token is silently ignored.** Rewriting
  `..._SL500_.jpg` → `..._SL2400_.jpg` returns HTTP 200 with a *still-500×500* image, so
  the URL looks HD and isn't. Apple's mzstatic genuinely re-renders: `100x100bb.jpg` →
  `2400x2400bb.jpg` really is 2400². Requesting `5000x5000bb.jpg` clamps to 2400² and
  returns byte-identical data, so 2400 is the real ceiling.
- **Open Library needs an explicit `fields=` list.** The default `search.json` response
  omits `ratings_average`, `ratings_count` and `number_of_pages_median` entirely — ask
  for them or they silently read as missing. Its `-L.jpg` cover is only 325×500, so it's
  a metadata source, not a cover source.
- **Apple returns foreign-language editions and unreleased re-issues** interleaved with
  the one you want (a `Proyecto Hail Mary` and a 2026-dated entry both appear for
  *Project Hail Mary*). `--reconcile` drops far-future entries when alternatives exist,
  but check `title` on the chosen cover.
- **Audible's `merchandising_summary` is HTML**, not plain text — strip tags before
  embedding (`--reconcile` already does).
- **The reconciled `isbn` is arbitrary, and often a foreign edition.** Open Library
  returns every edition's ISBN in one unordered list and `--reconcile` takes `[0]` —
  for *The Way of Kings* that's `9783453317109`, the German hardcover. Don't present it
  as "the" ISBN; if the user needs a specific edition's ISBN, pull the full list with
  `--source openlibrary` and pick deliberately.
- **`publisher` and `published_date` almost always conflict** between Audible (the
  audiobook publisher and its release date) and Open Library (the print publisher and
  first-publication year). Neither is wrong — they describe different editions. This is
  the single most common entry in `_conflicts`; resolve it by asking which edition
  matters rather than averaging them.
- **The cover you get may be a movie tie-in or re-issue**, not the edition the user
  means. The verified *Project Hail Mary* art is the "Now a major motion picture" Audible
  edition. Mention which edition you pulled.

## Guardrails

- Never bypass or auto-solve a CAPTCHA or bot-check — stop and hand it to the user.
- Never bulk-scrape or loop automated requests against amazon.com/audible.com — Tier 2 is
  one manual-style page visit per title, on demand only.
- This is for personal research/cataloging use, not for republishing or redistributing
  Amazon/Audible content at scale.
- If `exiftool` isn't installed, don't silently skip metadata embedding — tell the user and
  offer to install it (with permission) or save the metadata as a note instead.

## Troubleshooting

Only errors actually encountered:

- **`"google_error": "HTTP 429: Too Many Requests"`** — expected, not a bug. Google's
  shared anonymous quota. Proceed with the other three sources, or set
  `GOOGLE_BOOKS_API_KEY`. The script already annotates this error with the cause.
- **`_missing` contains `narrators`** — the title has no audiobook edition, or Audible's
  search didn't match it. Confirm with `--source audible` before telling the user there
  is no audiobook.
- **`_missing` contains `average_rating`/`ratings_count`/`isbn`** — these come only from
  Open Library. If its query missed, the book may be indexed under a variant title; retry
  without `--author`.
- **Cover downloads but is 500×500 when you expected 2400×2400** — you took the Audible
  URL rather than the Apple one. Re-check the `covers` array and pick by measured
  `width`, not by position.
- **Wrong volume of a series comes back** — you used raw mode and took `[0]`. Use
  `--reconcile`, and include the volume number in the title argument
  (`"Defiance of the Fall 6"`, not `"Defiance of the Fall"` + a mental note).

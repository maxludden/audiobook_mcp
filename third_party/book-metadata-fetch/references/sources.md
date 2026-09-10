# Metadata sources

## Tier 1 — free APIs, no auth, no ToS risk (always try first)

### Google Books API
`GET https://www.googleapis.com/books/v1/volumes?q=intitle:{title}+inauthor:{author}`
- Fields: `volumeInfo.{title, authors, publisher, publishedDate, description, pageCount,
  categories, averageRating, ratingsCount, industryIdentifiers, imageLinks, language,
  previewLink}`.
- `imageLinks` gives `smallThumbnail`/`thumbnail` (~128px) and sometimes
  `small`/`medium`/`large`/`extraLarge`. Even `extraLarge` is often only ~800px — usually
  NOT enough for "HD". Swap `zoom=1` → `zoom=0` and `&edge=curl` removal in the URL to get
  the unzoomed cover; still check pixel dimensions after download.
- No key needed for light usage; unauthenticated requests are rate-limited, so don't loop
  more than a handful of queries per minute.

### Open Library
`GET https://openlibrary.org/search.json?title={title}&author={author}`
- Fields: `docs[].{title, author_name, publisher, first_publish_year, subject, isbn,
  cover_i, ratings_average, ratings_count}`.
- Cover images: `https://covers.openlibrary.org/b/id/{cover_i}-L.jpg` (L = large, up to
  ~500px — still often not HD) or `https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg`.

### Audible catalog search (`api.audible.com`)
`GET https://api.audible.com/1.0/catalog/products?keywords={title}+{author}&response_groups=product_desc,product_attrs,contributors,media`
- This is the same public, unauthenticated JSON endpoint audible.com's own website calls
  client-side for search — not a scrape, no CAPTCHA surface, no login. Best source for
  audiobook-specific fields: `narrators[].name`, `runtime_length_min`, `publisher_name`,
  `release_date`, `merchandising_summary` (description), `asin`, and
  `product_images["500"]` (an `m.media-amazon.com` CDN URL).
- **Not Audnexus.** `api.audnex.us/books` looks similar but is ASIN-keyed only (it 400s
  with "Bad ASIN" on a title query) — it's a lookup-by-ASIN cache, not a search engine, so
  it isn't useful without already knowing the Audible ASIN. Don't use it for title search.
- Image caveat: the `_SL{n}_` size suffix in the CDN URL only upscales if a larger master
  exists for that asset — many audiobook cover images are capped at a 500x500 master and
  silently return the same bytes for `_SL1500_` as for `_SL500_`. Always check the
  downloaded file's actual pixel dimensions (e.g. `sips -g pixelWidth -g pixelHeight`
  on macOS) rather than trusting the URL or Content-Length.
- Still Amazon-owned infrastructure — keep to single on-demand lookups, not a bulk feed.

### Apple iTunes Search API (`itunes.apple.com`)
`GET https://itunes.apple.com/search?term={title}+{author}&entity=audiobook&limit=5`
(swap `entity=ebook` for print/Kindle-style editions if the audiobook entity doesn't match)
- Public, unauthenticated, no key. Fields: `collectionName`, `artistName`, `releaseDate`,
  `copyright`, `description`, `primaryGenreName`, `artworkUrl100`.
- **Best cover-art source found so far.** `artworkUrl100` lives on the `mzstatic.com` CDN
  and can be upsized by replacing the trailing `100x100bb.jpg` with e.g. `2400x2400bb.jpg`
  (`upsize_apple_artwork()` in lookup_book.py does this). Unlike Amazon's CDN — which often
  just clamps to a small cached master and returns identical bytes for any larger request —
  mzstatic has been observed serving genuinely distinct, larger pixel data up to ~2400x2400
  for real titles. Always verify actual downloaded pixel dimensions either way (some assets
  really do cap out smaller).
- Try this whenever the Audible/Google/OpenLibrary cover falls short of ~1000px — it's a
  Tier 1 call (JSON API, no scraping), so there's no reason to hold it back as a last resort.

## Tier 2 — retail product pages (fallback only, one lookup at a time; often unavailable)

Only reach for this when nothing in Tier 1 (including Apple) returned a usable cover or a
field Tier 1 can't provide (e.g. Amazon's own review count/star rating, "customers also
bought", a specific print edition's cover). Use the Claude Browser tool
(`mcp__Claude_Browser__*`), never a bulk HTTP scraper — but note that in practice the
Browser pane's domain policy has blocked essentially every major retailer tested:
amazon.com, audible.com, goodreads.com, books.apple.com, and simonandschuster.com have all
returned "blocked by policy" rather than loading. Don't fight this — treat Tier 2 as
unlikely to be available and fall back to telling the user what Tier 1 couldn't supply,
rather than trying another tool (e.g. Claude in Chrome / the user's real browser) to route
around a block without asking them first.

If a retail page *does* load for some domain:

1. Navigate to the product page for the specific title (search on-site if you don't have the
   direct URL already).
2. If a CAPTCHA, "verify you're human", or sign-in wall appears — **stop**. Tell the user and
   let them complete it manually; never attempt to solve or bypass it.
3. Read the page (`read_page` / `get_page_text`), don't click around beyond what's needed to
   land on the product page.
4. **Amazon-style cover images**: the product image element usually carries a
   `data-a-dynamic-image` attribute — a JSON object mapping image URLs to `[width, height]`
   pairs. Pick the URL with the largest `width`. Verify the downloaded file's actual pixel
   dimensions rather than trusting the URL or filename.
5. Download the single image URL directly (e.g. via `curl`) — don't screenshot-crop the page.
6. This is a single, on-demand lookup per book, not a crawl. Don't queue up dozens of titles
   against a retailer in one session — batch requests should stay on Tier 1 sources.

## Social / online presence (only if explicitly asked)

Use `WebSearch` or the `firecrawl-search` skill for `"{author name}" official site OR
twitter OR instagram OR goodreads author profile`. Report the top 2-3 links; don't crawl or
auto-follow them.

"""Pydantic input models for the audiobook_mcp tools."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class InspectEpubInput(BaseModel):
    """Input for previewing an EPUB's chapters/metadata without starting a job."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    epub_path: str = Field(
        ..., min_length=1,
        description="Absolute path to the .epub file, e.g. '/home/user/books/mark_of_the_fool_10.epub'."
    )


class StartConversionInput(BaseModel):
    """Input for creating (or resuming) an audiobook-to-M4B conversion job."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    mp3_path: str = Field(
        ..., min_length=1,
        description="Absolute path to the single-file audiobook (.mp3 or .m4a), e.g. "
                    "'/home/user/downloads/book.mp3'."
    )
    epub_path: str = Field(
        ..., min_length=1,
        description="Absolute path to the matching .epub for the same book. Used for chapter "
                    "titles, word-count sanity checks, and cover art."
    )
    out_dir: str = Field(
        ..., min_length=1,
        description="Directory the finished .m4b should be written into. Created if it doesn't exist."
    )
    min_gap: float = Field(
        default=3.5, ge=0.5, le=15.0,
        description="Minimum silence duration (seconds) to count as a chapter-transition gap. "
                    "The skill's default (3.5s) comfortably clears ordinary sentence pauses and "
                    "the ~1.5-3s decoy gap some publishers place a few seconds after the real cut."
    )
    bitrate: str = Field(
        default="96k",
        description="AAC bitrate for the output, e.g. '96k', '64k', '128k'. 96k is a good default "
                    "for spoken word."
    )
    intro_title: str = Field(default="Opening Credits", max_length=200,
                              description="Chapter title used for a detected opening segment.")
    outro_title: str = Field(default="End Credits", max_length=200,
                              description="Chapter title used for a detected closing segment.")
    outro_max_search: float = Field(
        default=1800.0, ge=60.0, le=7200.0,
        description="How far past the last chapter's start (seconds) to search for an outro gap."
    )
    detect_intro: bool = Field(
        default=False,
        description="Look for an opening-credits-style segment before chapter 1. Off by default: "
                    "most books have no intro, and a false positive silently shifts every chapter "
                    "label by one for the whole book. Only enable if the user has confirmed the "
                    "book has an audible intro."
    )
    intro_max_len: float = Field(
        default=300.0, ge=10.0, le=1800.0,
        description="A detected intro candidate is only accepted if it's under this many seconds."
    )
    detect_outro: bool = Field(
        default=False,
        description="Look for an end-credits-style segment after the last chapter. Off by default "
                    "for the same false-positive reason as detect_intro."
    )
    outro_max_tail: float = Field(
        default=300.0, ge=10.0, le=1800.0,
        description="A detected outro candidate is only accepted if the remaining audio after it "
                    "is under this many seconds."
    )
    force_restart: bool = Field(
        default=False,
        description="If a job for this exact mp3+epub+out_dir already exists, discard its progress "
                    "and start over from scratch instead of resuming it."
    )


class ContinueConversionInput(BaseModel):
    """Input for advancing an existing job by one bounded chunk of work."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    time_budget_seconds: float = Field(
        default=60.0, ge=5.0, le=300.0,
        description="Stop and checkpoint after roughly this many seconds of work. Re-call this "
                    "tool (or use audiobook_run_until_done) until the response's 'done' field is true."
    )


class RunUntilDoneInput(BaseModel):
    """Input for looping the pipeline forward until it finishes or a time cap is hit."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    chunk_seconds: float = Field(
        default=60.0, ge=5.0, le=300.0,
        description="Size of each internal work chunk, in seconds."
    )
    max_total_seconds: float = Field(
        default=1200.0, ge=10.0, le=3600.0,
        description="Give up and return whatever progress was made after roughly this many total "
                    "seconds, even if the job isn't finished yet. Call this tool again to keep going."
    )


class JobIdInput(BaseModel):
    """Input for tools that just need a job id."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")


class ListConversionsInput(BaseModel):
    """Input for listing known jobs."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    out_dir: Optional[str] = Field(
        default=None, description="If set, only list jobs whose output directory matches exactly."
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum jobs to return.")
    offset: int = Field(default=0, ge=0, description="Number of jobs to skip, for pagination.")


class InspectBoundaryInput(BaseModel):
    """Input for numerically inspecting the silence gaps around a chapter boundary."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    chapter_index: int = Field(
        ..., ge=1, description="1-based chapter number to inspect (matches EPUB chapter numbering)."
    )
    window_seconds: float = Field(
        default=30.0, ge=5.0, le=300.0,
        description="Half-width of the region (seconds) around the chapter's current recorded "
                    "boundary to search for silence gaps."
    )
    noise_db: float = Field(
        default=-30.0, ge=-60.0, le=-10.0,
        description="Loudness threshold (dB) below which audio counts as silence. Try a less "
                    "negative value (e.g. -25) for a passage with unusually loud background noise, "
                    "or more negative (e.g. -40) for a very quiet recording."
    )
    min_silence: float = Field(
        default=1.5, ge=0.2, le=15.0,
        description="Minimum duration (seconds) for a quiet stretch to be reported as a gap."
    )


class RenderWaveformInput(BaseModel):
    """Input for rendering a waveform snapshot around a chapter boundary."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    chapter_index: int = Field(..., ge=1, description="1-based chapter number to render around.")
    half_window_seconds: float = Field(
        default=8.0, ge=2.0, le=60.0,
        description="Half-width of the rendered window (seconds) centered on the chapter's "
                    "current recorded boundary."
    )
    center_override_seconds: Optional[float] = Field(
        default=None, ge=0.0,
        description="Render around this timestamp instead of the chapter's recorded boundary -- "
                    "useful after audiobook_inspect_chapter_boundary surfaces a candidate gap you "
                    "want to eyeball before patching."
    )


class PatchBoundaryInput(BaseModel):
    """Input for manually overriding a flagged or fallback chapter boundary."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    chapter_index: int = Field(..., ge=1, description="1-based chapter number to patch.")
    new_start_seconds: float = Field(
        ..., ge=0.0,
        description="The corrected start time (seconds into the source audio) for this chapter."
    )


class VerifyOutputInput(BaseModel):
    """Input for sanity-checking a finished M4B."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    m4b_path: str = Field(..., min_length=1, description="Absolute path to the .m4b file to verify.")
    source_audio_path: Optional[str] = Field(
        default=None,
        description="Absolute path to the original source .mp3/.m4a, if available, so total "
                    "duration can be cross-checked against it."
    )
    expected_chapter_count: Optional[int] = Field(
        default=None, ge=1,
        description="If known, the chapter count to compare against (e.g. from "
                    "audiobook_inspect_epub, +1 per detected intro/outro)."
    )


class CancelConversionInput(BaseModel):
    """Input for removing a job."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str = Field(..., min_length=1, description="Job id returned by audiobook_start_conversion.")
    delete_files: bool = Field(
        default=False,
        description="Also delete the job's working directory (extracted EPUB, encoded segments, "
                    "checkpoint state). Never touches the original mp3/epub or a already-delivered "
                    ".m4b in out_dir -- only the job's own scratch data under out_dir/.state/<job_id>."
    )


class MetadataSource(str, Enum):
    """Which metadata source(s) to query."""
    ALL = "all"
    GOOGLE = "google"
    OPENLIBRARY = "openlibrary"
    AUDIBLE = "audible"
    APPLE = "apple"


class LookupBookMetadataInput(BaseModel):
    """Input for looking up book/audiobook metadata and cover candidates
    across free, unauthenticated APIs (Google Books, Open Library, Audible's
    public catalog search, Apple iTunes Search). Personal research/cataloging
    use only -- see audiobook_lookup_book_metadata's docstring for the
    guardrails carried over from the source skill."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(
        ..., min_length=1, max_length=300,
        description="Book/audiobook title. Include a series volume number if known "
                    "(e.g. 'Defiance of the Fall 6', not just the series name) -- otherwise "
                    "a relevance-ranked search can hand back the wrong volume."
    )
    author: Optional[str] = Field(
        default=None, max_length=200,
        description="Author name, if known. Helps disambiguate common titles."
    )
    source: MetadataSource = Field(
        default=MetadataSource.ALL,
        description="Query all four sources (default) or just one, e.g. to retry after "
                    "another source errored."
    )
    reconcile: bool = Field(
        default=True,
        description="Merge all queried sources into one normalized record with "
                    "_provenance/_conflicts/_missing (recommended). If false, returns each "
                    "source's raw payload instead, for inspecting a specific field yourself."
    )
    include_covers: bool = Field(
        default=True,
        description="Also probe candidate cover image URLs and report their TRUE decoded "
                    "pixel dimensions (never trust a URL's size token -- some CDNs silently "
                    "clamp to a small cached master)."
    )
    google_api_key: Optional[str] = Field(
        default=None,
        description="Overrides the GOOGLE_BOOKS_API_KEY environment variable. Google Books' "
                    "anonymous quota is shared globally and usually returns HTTP 429 without "
                    "a key; the other three sources cover every field it would supply except "
                    "a long-form description, so this is optional."
    )


class FetchCoverImageInput(BaseModel):
    """Input for downloading a specific cover image URL (e.g. one returned
    by audiobook_lookup_book_metadata's `covers` array) to disk."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cover_url: str = Field(..., min_length=1, description="Absolute https/http URL of the cover image.")
    out_dir: str = Field(..., min_length=1, description="Directory to save the image into.")
    title: str = Field(..., min_length=1, max_length=300,
                        description="Book title, used to build the saved filename.")
    author: Optional[str] = Field(default=None, max_length=200,
                                   description="Author, used to build the saved filename, if known.")
    overwrite: bool = Field(
        default=False,
        description="If a file with the resulting name already exists, overwrite it. Default "
                    "false errors instead, matching the source skill's 'ask before overwriting'."
    )


class BookMetadataInput(BaseModel):
    """A book/audiobook metadata record to embed into a cover image, matching
    audiobook_lookup_book_metadata's reconciled `record` shape. All fields
    optional -- pass through whatever you have."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: Optional[str] = Field(default=None, max_length=300)
    authors: Optional[list[str]] = Field(default=None, max_length=20)
    narrators: Optional[list[str]] = Field(default=None, max_length=20)
    description: Optional[str] = Field(default=None, max_length=5000)
    publisher: Optional[str] = Field(default=None, max_length=200)
    copyright: Optional[str] = Field(default=None, max_length=300)
    published_date: Optional[str] = Field(default=None, max_length=20, description="e.g. 'YYYY-MM-DD' or 'YYYY'.")
    categories: Optional[list[str]] = Field(default=None, max_length=20)
    source_url: Optional[str] = Field(default=None, max_length=500)
    average_rating: Optional[float] = Field(default=None, ge=0, le=5)
    ratings_count: Optional[int] = Field(default=None, ge=0)
    length: Optional[str] = Field(default=None, max_length=50, description="e.g. '16h 10m' or '412 pages'.")


class EmbedCoverMetadataInput(BaseModel):
    """Input for writing a metadata record into a cover image's EXIF/XMP/IPTC tags."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    image_path: str = Field(..., min_length=1, description="Absolute path to the image file to tag.")
    metadata: BookMetadataInput = Field(..., description="The record to embed.")
    dry_run: bool = Field(
        default=False,
        description="If true, return the exiftool command that would run without writing anything."
    )

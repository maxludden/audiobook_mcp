#!/usr/bin/env python3
"""
audiobook_mcp -- MCP server that turns a single-file MP3/M4A audiobook plus
its EPUB into a properly chaptered M4B.

This wraps the audiobook-mp3-to-m4b skill's pipeline (real-silence chapter
alignment anchored on each chapter's own detected boundary, EPUB-sourced
titles/cover/metadata, word-count sanity checking) as a set of MCP tools
instead of a script an agent has to shell out to and re-invoke by hand.

Because a 20+ hour audiobook can take longer to process than most MCP
clients will happily block a single tool call for, conversion is exposed
as a resumable job: audiobook_start_conversion creates it,
audiobook_continue_conversion/audiobook_run_until_done advance it in
bounded chunks, and audiobook_get_status reads its progress without doing
any work. See pipeline.py's module docstring for the full rationale.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.types import ToolAnnotations

from . import cover_embed, metadata_fetch
from . import models as m
from .audio_probe import (
    AudioProbeError,
    FfmpegNotFoundError,
    detect_silences,
    render_waveform_png,
)
from .epub_extract import EpubExtractError, extract_epub
from .pipeline import Pipeline, PipelineConfig, PipelineError
from .registry import Registry, RegistryError
from .verify import verify_m4b

mcp = MCPServer("audiobook_mcp")
_registry = Registry()
_job_locks: dict[str, asyncio.Lock] = {}


def _lock_for(job_id: str) -> asyncio.Lock:
    lock = _job_locks.setdefault(job_id, asyncio.Lock())
    return lock


def _error(e: Exception) -> str:
    """Consistent error formatting across all tools -- returned as the
    tool's result text (not a protocol-level error) with an actionable
    message and, where useful, a hint about which tool to use instead."""
    if isinstance(e, RegistryError):
        return json.dumps({"error": str(e), "error_type": "unknown_job"}, indent=2)
    if isinstance(e, (FfmpegNotFoundError, cover_embed.ExiftoolNotFoundError)):
        return json.dumps({"error": str(e), "error_type": "dependency_missing"}, indent=2)
    if isinstance(e, (PipelineError, EpubExtractError, AudioProbeError,
                       metadata_fetch.MetadataLookupError, cover_embed.MetadataEmbedError)):
        return json.dumps({"error": str(e), "error_type": type(e).__name__}, indent=2)
    return json.dumps({"error": f"Unexpected error: {e}", "error_type": type(e).__name__}, indent=2)


def _pipeline_for(job_id: str) -> Pipeline:
    rec = _registry.get(job_id)
    return Pipeline.load_existing(Path(rec["work_dir"]))


# --------------------------------------------------------------------------- inspect_epub
@mcp.tool(
    name="audiobook_inspect_epub",
    title="Preview an EPUB's chapters and metadata",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_inspect_epub(params: m.InspectEpubInput) -> str:
    """Parse an EPUB and preview the book metadata, chapter list, and cover
    art the audiobook pipeline would use, WITHOUT touching any audio file
    or creating a conversion job. Use this before audiobook_start_conversion
    to sanity-check chapter detection (e.g. spot an EPUB with an unusual
    structure) or to answer questions like "how many chapters does this
    book have" without needing the MP3 yet.

    Args:
        params (InspectEpubInput):
            - epub_path (str): Absolute path to the .epub file.

    Returns:
        str: JSON with schema:
        {
          "title": str, "author": str, "series": str, "series_index": str,
          "year": str, "cover_found": bool,
          "chapter_count": int,
          "total_word_count": int,
          "first_chapters": [{"n": int, "title": str, "word_count": int}, ...],  # up to 3
          "last_chapters": [{"n": int, "title": str, "word_count": int}, ...]    # up to 3
        }
        or {"error": str, "error_type": str} on failure.

    Error Handling:
        - "epub_extract_error" style errors if the file isn't a valid EPUB
          or no chapters could be located at all (e.g. no TOC and no
          spine items pass the front/back-matter filter).
    """
    try:
        epub_path = Path(params.epub_path)
        import tempfile
        with tempfile.TemporaryDirectory(prefix="audiobook_mcp_inspect_") as td:
            result = extract_epub(epub_path, Path(td))
        chapters = result["chapters"]
        out = {
            **{k: v for k, v in result["meta"].items() if k in
               ("title", "author", "series", "series_index", "year")},
            "cover_found": bool(result["cover"]),
            "chapter_count": len(chapters),
            "total_word_count": sum(c["word_count"] for c in chapters),
            "first_chapters": [{"n": c["n"], "title": c["title"], "word_count": c["word_count"]}
                                for c in chapters[:3]],
            "last_chapters": [{"n": c["n"], "title": c["title"], "word_count": c["word_count"]}
                               for c in chapters[-3:]],
        }
        return json.dumps(out, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# ----------------------------------------------------------------------- start_conversion
@mcp.tool(
    name="audiobook_start_conversion",
    title="Start (or resume) an audiobook -> M4B conversion job",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_start_conversion(params: m.StartConversionInput, ctx: Context) -> str:
    """Create a resumable conversion job for one MP3/M4A audiobook + its
    EPUB, and run the fast first stage (EPUB parsing + audio duration
    probe). This does NOT do the slow chapter-alignment/encoding work --
    call audiobook_continue_conversion or audiobook_run_until_done next,
    and loop until the response's "done" field is true.

    Calling this again with the exact same mp3_path/epub_path/out_dir
    resumes the existing job (returning its current status) instead of
    creating a duplicate, unless force_restart=True.

    Args:
        params (StartConversionInput): see field descriptions. Notably:
            - mp3_path/epub_path (str): source files, must both exist.
            - out_dir (str): where the finished .m4b will be written.
            - cover_image_path (Optional[str]): use this image instead of
              the EPUB's own cover. Typical flow: audiobook_lookup_book_metadata
              -> audiobook_fetch_cover_image -> pass the downloaded path here.
            - detect_intro/detect_outro (bool): off by default; only turn
              on if the user has confirmed the book has an audible
              intro/outro segment (see tool description in code for why).

    Returns:
        str: JSON with schema:
        {
          "job_id": str,               # pass this to every other job tool
          "stage": str,                 # "align" once extract succeeds
          "book": {"title", "author", "series", "series_index",
                    "chapter_count", "cover_found"},
          "next_step": str             # what to call next
        }
        or {"error": str, "error_type": str} on failure.

    Error Handling:
        - "ffmpeg_missing" if ffmpeg/ffprobe aren't on PATH.
        - Otherwise a PipelineError/EpubExtractError with a specific
          reason (file not found, wrong extension, unparseable EPUB, ...).
    """
    try:
        mp3, epub, out_dir = Path(params.mp3_path), Path(params.epub_path), Path(params.out_dir)
        cover_override = Path(params.cover_image_path) if params.cover_image_path else None
        PipelineConfig.validate_new(mp3, epub, out_dir, cover_override=cover_override)

        job_id, work_dir = _registry.create_job(mp3=mp3, epub=epub, out_dir=out_dir,
                                                  title_hint=epub.stem)
        if params.force_restart:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

        config = PipelineConfig(
            mp3=mp3, epub=epub, out_dir=out_dir, min_gap=params.min_gap, bitrate=params.bitrate,
            intro_title=params.intro_title, outro_title=params.outro_title,
            outro_max_search=params.outro_max_search, detect_intro=params.detect_intro,
            intro_max_len=params.intro_max_len, detect_outro=params.detect_outro,
            outro_max_tail=params.outro_max_tail, cover_override=cover_override,
        )
        pipeline = Pipeline(config, work_dir)

        async with _lock_for(job_id):
            if pipeline.state["stage"] == "extract":
                await asyncio.to_thread(pipeline.run, 60.0, lambda msg: ctx.info(msg))
            status = pipeline.summarize()

        next_step = ("Call audiobook_continue_conversion or audiobook_run_until_done with "
                     "this job_id to keep processing." if not status["done"] else
                     "Already done -- see final_output.")
        return json.dumps({"job_id": job_id, "stage": status["stage"], "book": status["book"],
                            "next_step": next_step}, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# -------------------------------------------------------------------- continue_conversion
@mcp.tool(
    name="audiobook_continue_conversion",
    title="Advance a conversion job by one bounded chunk of work",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=False,
    ),
)
async def audiobook_continue_conversion(params: m.ContinueConversionInput, ctx: Context) -> str:
    """Do up to time_budget_seconds of work on a job (aligning chapters,
    encoding segments, assembling, or delivering the final file --
    whichever stage it's currently in), checkpoint to disk, and return.
    Re-call this with the same job_id, or use audiobook_run_until_done to
    loop automatically, until the response's "done" field is true.

    Safe to call again immediately if a previous call errored partway
    through a stage -- work already checkpointed is never redone.

    Args:
        params (ContinueConversionInput):
            - job_id (str): from audiobook_start_conversion.
            - time_budget_seconds (float): stop and checkpoint after
              roughly this long (5-300s, default 60).

    Returns:
        str: JSON status object -- see audiobook_get_status's Returns
        schema, which this shares exactly.

    Error Handling:
        - "unknown_job" if job_id isn't registered.
        - ffmpeg/encoding failures surface with ffmpeg's own error text
          tail included; the job's checkpoint is untouched so retrying
          (after e.g. freeing disk space) picks up where it left off.
    """
    try:
        pipeline = _pipeline_for(params.job_id)
        async with _lock_for(params.job_id):
            status = await asyncio.to_thread(
                pipeline.run, params.time_budget_seconds, lambda msg: ctx.info(msg)
            )
        await ctx.report_progress(
            progress=status["overall_progress"], total=1.0, message=status["status_line"],
        )
        return json.dumps(status, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# ---------------------------------------------------------------------------- run_until_done
@mcp.tool(
    name="audiobook_run_until_done",
    title="Loop a conversion job forward until it finishes or a time cap is hit",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=False,
    ),
)
async def audiobook_run_until_done(params: m.RunUntilDoneInput, ctx: Context) -> str:
    """Repeatedly advance a job in chunk_seconds increments, reporting
    progress, until either it finishes or max_total_seconds elapses --
    whichever comes first. This is the convenient one-call way to drive a
    job; use audiobook_continue_conversion directly instead if you want
    fine-grained control over each chunk (e.g. to react to a warning
    partway through).

    For a long book this may still return with "done": false if
    max_total_seconds wasn't enough -- just call it again with the same
    job_id to keep going; already-completed work is never redone.

    Args:
        params (RunUntilDoneInput):
            - job_id (str): from audiobook_start_conversion.
            - chunk_seconds (float): size of each internal step (default 60).
            - max_total_seconds (float): overall cap for this call (default
              1200s / 20 min).

    Returns:
        str: JSON status object (same schema as audiobook_get_status) plus:
            - "elapsed_seconds" (float): how long this call actually ran.
            - "chunks_run" (int): how many internal chunks it took.

    Error Handling:
        - Same as audiobook_continue_conversion. If a chunk raises partway
          through the loop, the loop stops immediately and the error is
          returned -- prior chunks in this call are already checkpointed.
    """
    try:
        pipeline = _pipeline_for(params.job_id)
        t_start = time.monotonic()
        chunks_run = 0
        status = pipeline.summarize()
        async with _lock_for(params.job_id):
            while not status["done"]:
                elapsed = time.monotonic() - t_start
                remaining = params.max_total_seconds - elapsed
                if remaining <= 0:
                    break
                budget = min(params.chunk_seconds, remaining)
                t_chunk = time.monotonic()
                status = await asyncio.to_thread(pipeline.run, budget, lambda msg: ctx.info(msg))
                chunk_dt = time.monotonic() - t_chunk
                chunks_run += 1
                # Report the whole-job fraction, not elapsed/max_total_seconds --
                # a client that reads the numeric progress/total (rather than
                # parsing the message string) would otherwise see 100% at the
                # end of every call just because its own time cap was reached,
                # even when the conversion itself is nowhere near done.
                call_elapsed = round(elapsed + chunk_dt, 1)
                await ctx.report_progress(
                    progress=status["overall_progress"], total=1.0,
                    message=f"{status['status_line']} "
                            f"(elapsed {call_elapsed}s/{params.max_total_seconds}s this call)",
                )
                if chunk_dt < 0.05 or chunks_run >= 2000:
                    # stage did essentially no work (shouldn't normally
                    # happen once "done") -- bail rather than spin
                    break
        status["elapsed_seconds"] = round(time.monotonic() - t_start, 1)
        status["chunks_run"] = chunks_run
        return json.dumps(status, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# ------------------------------------------------------------------------------- get_status
@mcp.tool(
    name="audiobook_get_status",
    title="Read a conversion job's current status",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_get_status(params: m.JobIdInput) -> str:
    """Read a job's current progress WITHOUT doing any work -- safe to
    call as often as you like, including while another
    audiobook_continue_conversion/audiobook_run_until_done call might be
    in flight.

    Args:
        params (JobIdInput):
            - job_id (str): from audiobook_start_conversion.

    Returns:
        str: JSON with schema:
        {
          "stage": str,          # "extract"|"align"|"encode"|"assemble"|"deliver"|"done"
          "done": bool,
          "status_line": str,     # most recent human-readable progress line
          "overall_progress": float,      # 0.0-1.0 across the WHOLE job, comparable
                                            # across separate continue_conversion/
                                            # run_until_done calls -- suitable for a
                                            # progress bar. Weighted by each stage's
                                            # typical share of total time (encode
                                            # dominates), not just "stages done / 6".
          "overall_progress_pct": float,  # overall_progress * 100, rounded to 1 decimal
          "book": {"title", "author", "series", "series_index",
                    "chapter_count", "cover_found"} | null,
          "alignment": {          # present once alignment has started
            "chapters_aligned": int, "chapters_total": int,
            "low_confidence_chapters": [int, ...],   # spot-check these
            "fallback_chapters": [int, ...],          # these need a fix
            "manual_overrides": [int, ...],
            "outro_detected": bool
          } | absent,
          "encoding": {"segments_encoded": int, "segments_total": int} | absent,
          "word_count_warnings": [
            {"n": int, "title": str, "actual_duration_s": float, "expected_duration_s": float}
          ] | absent,             # segments whose duration deviates >30% from EPUB word-count pace
          "assembled_path": str | absent,
          "delivery": {"bytes_done": int, "bytes_total": int} | absent,
          "final_output": str | absent,   # present once stage == "done"
          "recent_log": [str, ...]        # last 10 status lines
        }
        or {"error": str, "error_type": str} if job_id is unknown.

    Examples:
        - Use when: checking whether a long-running job has finished yet.
        - Use when: deciding whether fallback_chapters/word_count_warnings
          need audiobook_inspect_chapter_boundary + audiobook_patch_chapter_boundary
          before trusting the output.
    """
    try:
        pipeline = _pipeline_for(params.job_id)
        return json.dumps(pipeline.summarize(), indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# --------------------------------------------------------------------------- list_conversions
@mcp.tool(
    name="audiobook_list_conversions",
    title="List known conversion jobs",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_list_conversions(params: m.ListConversionsInput) -> str:
    """List jobs known to this server (persisted at
    $AUDIOBOOK_MCP_HOME/registry.json, default ~/.audiobook_mcp/), most
    recently created first.

    Args:
        params (ListConversionsInput):
            - out_dir (Optional[str]): if set, only jobs whose output
              directory matches exactly.
            - limit (int): max jobs to return (1-100, default 20).
            - offset (int): pagination offset (default 0).

    Returns:
        str: JSON with schema:
        {
          "total": int, "count": int, "offset": int,
          "jobs": [
            {"job_id": str, "mp3": str, "epub": str, "out_dir": str,
             "work_dir": str, "created_at": float}
          ],
          "has_more": bool, "next_offset": int | null
        }
    """
    out_dir = Path(params.out_dir) if params.out_dir else None
    result = _registry.list(out_dir=out_dir, limit=params.limit, offset=params.offset)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- inspect_chapter_boundary
@mcp.tool(
    name="audiobook_inspect_chapter_boundary",
    title="List silence gaps found around a chapter's recorded boundary",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_inspect_chapter_boundary(params: m.InspectBoundaryInput) -> str:
    """Re-scan the audio around a chapter's currently recorded boundary
    and list every silence gap found, so you (or the user) can judge
    whether the recorded boundary is the right one -- most useful for a
    chapter that showed up in fallback_chapters or word_count_warnings
    from audiobook_get_status. Does not change anything; follow up with
    audiobook_render_boundary_waveform for a visual, or
    audiobook_patch_chapter_boundary once you've picked the right gap.

    Args:
        params (InspectBoundaryInput):
            - job_id (str), chapter_index (int, 1-based).
            - window_seconds (float): half-width of the search region
              around the current boundary (default 30).
            - noise_db / min_silence (float): silencedetect thresholds --
              widen noise_db (e.g. -25) for a loud passage, shorten
              min_silence to catch briefer pauses.

    Returns:
        str: JSON with schema:
        {
          "chapter_index": int,
          "current_boundary_seconds": float,
          "search_window": {"start": float, "end": float},
          "gaps_found": [
            {"start": float, "end": float, "duration": float,
             "distance_from_current_boundary": float}
          ]   # sorted by distance from the current boundary
        }
        or {"error": ...} if the chapter hasn't been aligned yet.

    Error Handling:
        - Raises a clear error if chapter_index hasn't been reached by
          alignment yet (check audiobook_get_status's "alignment" field
          first).
    """
    try:
        pipeline = _pipeline_for(params.job_id)
        align = pipeline.state.get("align")
        if not align or str(params.chapter_index) not in align.get("boundaries", {}):
            raise PipelineError(
                f"Chapter {params.chapter_index} hasn't been aligned yet. Check "
                "audiobook_get_status's 'alignment.chapters_aligned' first."
            )
        boundary = align["boundaries"][str(params.chapter_index)]
        start = max(0.0, boundary - params.window_seconds)
        end = boundary + params.window_seconds
        intervals = await asyncio.to_thread(
            detect_silences, pipeline.config.mp3, start, end - start,
            params.noise_db, params.min_silence,
        )
        gaps = sorted(
            [{"start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3),
              "distance_from_current_boundary": round(abs(((s + e) / 2) - boundary), 3)}
             for s, e in intervals],
            key=lambda g: g["distance_from_current_boundary"],
        )
        return json.dumps({
            "chapter_index": params.chapter_index,
            "current_boundary_seconds": round(boundary, 3),
            "search_window": {"start": round(start, 3), "end": round(end, 3)},
            "gaps_found": gaps,
        }, indent=2)
    except Exception as e:
        return _error(e)


# ------------------------------------------------------------- render_boundary_waveform
@mcp.tool(
    name="audiobook_render_boundary_waveform",
    title="Render a waveform image around a chapter boundary",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_render_boundary_waveform(params: m.RenderWaveformInput) -> Image:
    """Render a PNG waveform snapshot centered on a chapter's current
    recorded boundary (or on center_override_seconds, e.g. a candidate gap
    surfaced by audiobook_inspect_chapter_boundary). A clean cut shows an
    obvious gap centered in the image with speech clusters on both sides;
    if the gap looks off-center or absent, the boundary likely needs
    audiobook_patch_chapter_boundary.

    Args:
        params (RenderWaveformInput):
            - job_id (str), chapter_index (int, 1-based).
            - half_window_seconds (float): half-width of the rendered
              window (default 8).
            - center_override_seconds (Optional[float]): render around
              this timestamp instead of the recorded boundary.

    Returns:
        Image: a PNG waveform image.

    Error Handling:
        - Raises a clear error if chapter_index hasn't been aligned yet
          and no center_override_seconds was given.
    """
    pipeline = _pipeline_for(params.job_id)
    center = params.center_override_seconds
    if center is None:
        align = pipeline.state.get("align")
        if not align or str(params.chapter_index) not in align.get("boundaries", {}):
            raise PipelineError(
                f"Chapter {params.chapter_index} hasn't been aligned yet, and no "
                "center_override_seconds was given. Check audiobook_get_status's "
                "'alignment.chapters_aligned' first, or pass an explicit timestamp."
            )
        center = align["boundaries"][str(params.chapter_index)]
    out_path = (pipeline.work_dir / "waveforms" /
                f"ch{params.chapter_index:03d}_{int(center)}.png")
    await asyncio.to_thread(
        render_waveform_png, pipeline.config.mp3, center, params.half_window_seconds, out_path,
    )
    return Image(data=out_path.read_bytes(), format="png")


# ------------------------------------------------------------------- patch_chapter_boundary
@mcp.tool(
    name="audiobook_patch_chapter_boundary",
    title="Manually override a chapter's detected boundary",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True,
        idempotent_hint=False, open_world_hint=False,
    ),
)
async def audiobook_patch_chapter_boundary(params: m.PatchBoundaryInput, ctx: Context) -> str:
    """Manually fix a chapter boundary that landed in the wrong place
    (surfaced via fallback_chapters, low_confidence_chapters, or
    word_count_warnings). This overwrites the recorded boundary for
    chapter_index and invalidates every stage derived from it -- encoded
    segments, the assembled file, and delivery all get regenerated on the
    next audiobook_continue_conversion/audiobook_run_until_done call.
    Chapters before and after chapter_index are untouched, so this is
    always cheaper than restarting the whole job.

    Args:
        params (PatchBoundaryInput):
            - job_id (str), chapter_index (int, 1-based).
            - new_start_seconds (float): the corrected boundary, typically
              the midpoint of a gap surfaced by
              audiobook_inspect_chapter_boundary.

    Returns:
        str: JSON with schema:
        {
          "chapter_index": int,
          "old_start_seconds": float, "new_start_seconds": float,
          "invalidated": [str, ...],   # state keys cleared, e.g. "segments", "encode_progress"
          "stage_now": str             # call audiobook_continue_conversion next
        }
        or {"error": ...} if chapter_index hasn't been aligned yet or
        new_start_seconds is out of range.
    """
    try:
        pipeline = _pipeline_for(params.job_id)
        async with _lock_for(params.job_id):
            result = pipeline.patch_boundary(params.chapter_index, params.new_start_seconds)
        ctx.info(f"patched chapter {params.chapter_index} for job {params.job_id}")
        return json.dumps(result, indent=2)
    except Exception as e:
        return _error(e)


# ------------------------------------------------------------------------------ verify_output
@mcp.tool(
    name="audiobook_verify_output",
    title="Sanity-check a finished M4B",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_verify_output(params: m.VerifyOutputInput) -> str:
    """Run the same checks the skill recommends before handing a result
    back: chapter count, total duration (optionally cross-checked against
    the original source file), first/last chapter titles, and whether a
    cover-art video stream is present.

    Args:
        params (VerifyOutputInput):
            - m4b_path (str): the file to verify.
            - source_audio_path (Optional[str]): original mp3/m4a, for a
              duration cross-check.
            - expected_chapter_count (Optional[int]): compare against this.

    Returns:
        str: JSON with schema:
        {
          "duration_seconds": float,
          "chapter_count": int,
          "first_chapter_title": str, "last_chapter_title": str,
          "has_cover_art": bool,
          "source_duration_seconds": float | absent,
          "duration_difference_seconds": float | absent,
          "issues": [str, ...]    # empty list means everything checked out
        }
        or {"error": ...} if m4b_path can't be probed by ffprobe.
    """
    try:
        result = await asyncio.to_thread(
            verify_m4b,
            Path(params.m4b_path),
            Path(params.source_audio_path) if params.source_audio_path else None,
            params.expected_chapter_count,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# --------------------------------------------------------------------------- cancel_conversion
@mcp.tool(
    name="audiobook_cancel_conversion",
    title="Remove a conversion job",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_cancel_conversion(params: m.CancelConversionInput) -> str:
    """Remove a job from the registry. By default this only forgets the
    job_id (the on-disk state under out_dir/.state/<job_id> is left
    alone, so audiobook_start_conversion with the same mp3/epub/out_dir
    would still resume it). With delete_files=True, also deletes that
    scratch directory -- the original mp3/epub and anything already
    delivered into out_dir itself are never touched either way.

    Args:
        params (CancelConversionInput):
            - job_id (str).
            - delete_files (bool): also delete the job's working directory.

    Returns:
        str: JSON {"job_id": str, "removed_from_registry": true, "files_deleted": bool}
        or {"error": ...} if job_id is unknown.
    """
    try:
        rec = _registry.delete(params.job_id)
        files_deleted = False
        if params.delete_files:
            import shutil
            shutil.rmtree(Path(rec["work_dir"]), ignore_errors=True)
            files_deleted = True
        return json.dumps({"job_id": params.job_id, "removed_from_registry": True,
                            "files_deleted": files_deleted}, indent=2)
    except Exception as e:
        return _error(e)


# -------------------------------------------------------------------- lookup_book_metadata
@mcp.tool(
    name="audiobook_lookup_book_metadata",
    title="Look up book/audiobook metadata and cover art candidates",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=True,
    ),
)
async def audiobook_lookup_book_metadata(params: m.LookupBookMetadataInput) -> str:
    """Look up book/audiobook metadata (authors, narrators, publisher,
    publish date, description, ratings, ISBN, length) and candidate cover
    images across free, unauthenticated APIs: Google Books, Open Library,
    Audible's public catalog search, and Apple's iTunes Search. No
    scraping, login, or CAPTCHA surface is touched -- these are the same
    plain JSON endpoints each service's own search box calls.

    Ported from the book-metadata-fetch skill; carries over its
    guardrails: personal research/cataloging use, not a bulk data feed --
    this tool self-throttles slightly between calls, but don't loop it
    over long lists of titles. It has NO Amazon/Audible-retail-page
    fallback (that requires a browser tool this server doesn't have) --
    if Tier 1 here comes up short, that's the tool's real ceiling; a
    calling agent with its own browser tool may attempt one manual,
    on-demand page visit per the source skill's Tier 2 procedure, subject
    to the same guardrails (never bypass a CAPTCHA/sign-in wall).

    Series volumes are the main failure mode: search APIs rank by
    relevance, not series order, so an unfiltered query for "Defiance of
    the Fall" can return book 12. Include the volume number in `title`
    when known (e.g. "Defiance of the Fall 6").

    Args:
        params (LookupBookMetadataInput): see field descriptions. Notably
            reconcile=True (default) merges all sources into one record;
            include_covers=True (default) probes each candidate cover's
            real pixel dimensions (never trust a CDN URL's size token).

    Returns:
        str: JSON. With reconcile=True:
        {
          "record": {
            "title": str, "authors": [str,...], "narrators": [str,...],
            "description": str, "publisher": str, "published_date": str,
            "categories": [str,...], "average_rating": float,
            "ratings_count": int, "length": str, "isbn": str,
            "source_url": str,
            "_provenance": {field: source_name},   # which source supplied each field
            "_conflicts": [str, ...],  # fields sources disagreed on -- READ THESE
                                        # before trusting publisher/published_date;
                                        # audiobook vs. print edition is the common case
            "_missing": [str, ...]     # fields nothing supplied
          },
          "errors": {source_name: error_message},
          "covers": [                  # present if include_covers=True
            {"source": str, "url": str, "width": int|null, "height": int|null,
             "bytes": int} | {"source": str, "url": str, "error": str}
          ]
        }
        With reconcile=False: {"raw": {source_name: [...]}, "errors": {...}, "covers": [...]}.

    Error Handling:
        - Per-source failures land in "errors" rather than failing the
          whole call -- e.g. Google Books commonly 429s on its shared
          anonymous quota; the other three sources still return.
        - Only fails outright ({"error": ...}) if input validation fails.
    """
    try:
        sources = list(m.MetadataSource) if params.source == m.MetadataSource.ALL else [params.source]
        source_names = [s.value for s in sources if s != m.MetadataSource.ALL]
        api_key = params.google_api_key or os.environ.get("GOOGLE_BOOKS_API_KEY")
        result = await asyncio.to_thread(
            metadata_fetch.lookup_book_metadata,
            params.title, params.author, source_names,
            params.reconcile, params.include_covers, api_key,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return _error(e)


# ----------------------------------------------------------------------- fetch_cover_image
@mcp.tool(
    name="audiobook_fetch_cover_image",
    title="Download a cover image URL to disk",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=True,
    ),
)
async def audiobook_fetch_cover_image(params: m.FetchCoverImageInput) -> str:
    """Download a specific cover image URL (typically one from
    audiobook_lookup_book_metadata's `covers` array -- pick by measured
    width/height, not source order or URL) to out_dir, and report its
    real decoded pixel dimensions. Saved as
    "<Sanitized-Title>_<Sanitized-Author>.jpg" (or without the author
    suffix if none given), matching the source skill's naming convention.

    Args:
        params (FetchCoverImageInput):
            - cover_url (str): the image URL to download.
            - out_dir (str): destination directory (created if missing).
            - title/author (str): used to build the filename.
            - overwrite (bool): default False errors instead of
              clobbering an existing file with the same name.

    Returns:
        str: JSON {"path": str, "width": int|null, "height": int|null, "bytes": int}
        or {"error": ...} on failure.

    Error Handling:
        - Errors (not overwrites) if a file with the computed name
          already exists and overwrite=False.
        - Network failures surface with the underlying reason.
    """
    try:
        out_dir = Path(params.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = cover_embed.sanitize_filename(params.title)
        if params.author:
            name += f"_{cover_embed.sanitize_filename(params.author)}"
        dest = out_dir / f"{name}.jpg"
        if dest.exists() and not params.overwrite:
            raise cover_embed.MetadataEmbedError(
                f"{dest} already exists. Pass overwrite=true to replace it, or choose a "
                "different out_dir/title."
            )
        result = await asyncio.to_thread(metadata_fetch.download_to_file, params.cover_url, dest)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _error(e)


# ------------------------------------------------------------------- embed_cover_metadata
@mcp.tool(
    name="audiobook_embed_cover_metadata",
    title="Embed a metadata record into a cover image's EXIF/XMP/IPTC tags",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True,
        idempotent_hint=True, open_world_hint=False,
    ),
)
async def audiobook_embed_cover_metadata(params: m.EmbedCoverMetadataInput) -> str:
    """Write a book metadata record into an existing image file's
    EXIF/XMP/IPTC tags via exiftool (title, creator/artist, description,
    publisher, copyright, date, keywords, source URL, rating). Narrator
    and length have no standard EXIF equivalent, so the complete record
    is also stamped into the JPEG Comment field as JSON so nothing is
    lost. Overwrites image_path's own metadata in place -- the image's
    pixel data is untouched.

    Requires exiftool on PATH; if it's missing this tool errors rather
    than silently skipping the embed (per the source skill's guardrail).

    Args:
        params (EmbedCoverMetadataInput):
            - image_path (str): the image file to tag (typically the
              output of audiobook_fetch_cover_image).
            - metadata (BookMetadataInput): the record to embed, e.g.
              straight from audiobook_lookup_book_metadata's "record".
            - dry_run (bool): if true, return the exiftool command
              without writing anything.

    Returns:
        str: JSON {"command": [str, ...], "output": str|null, "dry_run": bool}
        or {"error": ...} if image_path doesn't exist or exiftool fails/is missing.
    """
    try:
        result = await asyncio.to_thread(
            cover_embed.embed_metadata,
            Path(params.image_path),
            params.metadata.model_dump(exclude_none=True),
            params.dry_run,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        return _error(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

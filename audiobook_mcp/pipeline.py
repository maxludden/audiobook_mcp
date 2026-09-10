"""
Resumable MP3/M4A (+ EPUB) -> chaptered M4B pipeline.

This is a refactor of the audiobook-mp3-to-m4b skill's scripts/run.py into
a library the MCP server can drive one bounded chunk at a time. The core
algorithm is unchanged; what changed is packaging:

  * No stdout printing (this runs inside an MCP stdio server -- stdout is
    the protocol stream). Every stage appends a human-readable status line
    to `state["log"]` and returns a structured status dict instead.
  * `state.json` also stores the job's full config (source paths, options),
    so a job can be rehydrated from just its work_dir after a server
    restart, without needing the original tool-call arguments again.
  * The final-output copy ("deliver") stage writes into a per-job scratch
    directory instead of a shared global /tmp path, so two jobs for books
    with the same title can't collide.
  * A `patch_boundary()` method implements the skill's documented manual
    chapter-boundary fix as a callable operation instead of an
    edit-state.json-by-hand recipe.

Why chunked/resumable at all: even outside the specific 45s sandbox cap
the skill was written for, a single MCP tool call blocking for the tens of
minutes a 20+ hour audiobook can take to fully process is a poor fit for
most MCP clients. Bounding each call to a time budget and checkpointing to
disk after every unit of progress means the calling agent can loop
`continue_conversion` (or call `run_until_done`, which loops internally)
regardless of how the client's own timeouts are configured.

Chapter-boundary detection algorithm (unchanged from the skill): don't
trust a bookseller's displayed per-chapter duration (rounded to the
second, drifts over a long book) and don't anchor purely on EPUB word
counts either (dialogue-heavy vs. descriptive passages read at different
paces). Instead, chain forward from the previous chapter's *actual
detected* boundary and take the first qualifying silence gap after it.
Every step's anchor is ground truth from the audio itself, so a bad
detection can't drag down the chapters after it. Many audiobooks place a
short decoy gap a few seconds after the real transition (a beat before a
chapter-announcement sting); `min_gap` filters that out.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from .audio_probe import (
    check_ffmpeg_available,
    detect_silences,
    ffprobe_duration,
)
from .encode_segments import encode_one, safe_name
from .epub_extract import EpubExtractError, extract_epub

STAGES = ("extract", "align", "encode", "assemble", "deliver", "done")
_MAX_LOG_LINES = 50
# Rough share of a long book's total wall-clock cost each stage typically
# takes, used only to compute a single overall_progress fraction for
# progress bars / ctx.report_progress -- NOT a per-book time estimate.
# Encode (decoding + re-encoding the entire audio) dominates; extract,
# align (short silencedetect clips, now parallel-friendly window search),
# and assemble (stream-copy concat/mux, no re-encode) are comparatively
# quick; deliver (copying the whole finished file) can be non-trivial on
# a slow disk or network mount.
_STAGE_WEIGHTS = {"extract": 0.02, "align": 0.13, "encode": 0.55, "assemble": 0.05, "deliver": 0.25}
# Segment encodes are independent, single-threaded ffmpeg subprocesses, so
# running several at once uses more of a multi-core host instead of
# leaving it idle while one segment encodes at a time. Capped well below
# "one per core" so a huge core count doesn't turn into dozens of
# concurrent ffmpeg processes fighting over disk I/O.
_MAX_ENCODE_WORKERS = 4

ProgressCB = Callable[[str], None] | None


def slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:80] or "book"


class PipelineError(RuntimeError):
    """A pipeline stage failed in a way that needs user attention."""


@dataclass
class PipelineConfig:
    mp3: Path
    epub: Path
    out_dir: Path
    min_gap: float = 3.5
    intro_title: str = "Opening Credits"
    outro_title: str = "End Credits"
    outro_max_search: float = 1800.0
    bitrate: str = "96k"
    detect_intro: bool = False
    intro_max_len: float = 300.0
    detect_outro: bool = False
    outro_max_tail: float = 300.0
    cover_override: Path | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["mp3"] = str(self.mp3)
        d["epub"] = str(self.epub)
        d["out_dir"] = str(self.out_dir)
        d["cover_override"] = str(self.cover_override) if self.cover_override else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PipelineConfig:
        d = dict(d)
        d["mp3"] = Path(d["mp3"])
        d["epub"] = Path(d["epub"])
        d["out_dir"] = Path(d["out_dir"])
        d["cover_override"] = Path(d["cover_override"]) if d.get("cover_override") else None
        return cls(**d)

    @staticmethod
    def validate_new(mp3: Path, epub: Path, out_dir: Path,
                      cover_override: Path | None = None) -> None:
        """Raise PipelineError with an actionable message if the inputs
        can't plausibly be processed. Called before a job is created."""
        check_ffmpeg_available()
        if not mp3.exists():
            raise PipelineError(f"Audiobook file not found: {mp3}")
        if not mp3.is_file():
            raise PipelineError(f"Audiobook path is not a file: {mp3}")
        if mp3.suffix.lower() not in (".mp3", ".m4a", ".m4b"):
            raise PipelineError(
                f"Expected a .mp3 or .m4a audiobook file, got {mp3.suffix!r} ({mp3}). "
                "If this really is audio, rename it with the correct extension."
            )
        if not epub.exists():
            raise PipelineError(f"EPUB not found: {epub}")
        if not epub.is_file():
            raise PipelineError(f"EPUB path is not a file: {epub}")
        if epub.suffix.lower() != ".epub":
            raise PipelineError(f"Expected a .epub file, got {epub.suffix!r} ({epub}).")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise PipelineError(f"Can't create output directory {out_dir}: {e}") from e
        if cover_override is not None:
            if not cover_override.exists():
                raise PipelineError(f"Cover image not found: {cover_override}")
            if not cover_override.is_file():
                raise PipelineError(f"Cover image path is not a file: {cover_override}")
            if cover_override.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                raise PipelineError(
                    f"Expected a .jpg/.jpeg/.png cover image, got {cover_override.suffix!r} "
                    f"({cover_override})."
                )


class Pipeline:
    def __init__(self, config: PipelineConfig, work_dir: Path):
        self.config = config
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "state.json"
        self.state = self._load_state()

    # ------------------------------------------------------------- state io
    def _load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"stage": "extract", "config": self.config.to_dict(), "log": []}

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))

    def _log(self, msg: str, cb: ProgressCB = None) -> None:
        log = self.state.setdefault("log", [])
        log.append(msg)
        del log[:-_MAX_LOG_LINES]
        self.state["status_line"] = msg
        if cb is not None:
            cb(msg)

    @classmethod
    def load_existing(cls, work_dir: Path) -> Pipeline:
        state_path = work_dir / "state.json"
        if not state_path.exists():
            raise PipelineError(f"No job state found at {work_dir}")
        state = json.loads(state_path.read_text())
        config = PipelineConfig.from_dict(state["config"])
        return cls(config, work_dir)

    # ---------------------------------------------------------------- extract
    def stage_extract(self, budget: float, cb: ProgressCB = None) -> bool:
        try:
            result = extract_epub(self.config.epub, self.work_dir / "epub")
        except EpubExtractError as e:
            raise PipelineError(str(e)) from e
        cover_note = "yes" if result["cover"] else "no"
        if self.config.cover_override:
            result["cover"] = str(self.config.cover_override)
            cover_note = "override"
        self.state["epub"] = result
        self.state["mp3_duration"] = ffprobe_duration(self.config.mp3)
        self.state["stage"] = "align"
        self.state["align"] = {"boundaries": {}, "next": 1, "low_confidence": [],
                                "fallback": [], "manual_overrides": []}
        self._save()
        self._log(
            f"extracted: {result['meta']['title']} by {result['meta']['author']}, "
            f"{len(result['chapters'])} chapters, cover={cover_note}",
            cb,
        )
        return True

    # ------------------------------------------------------------------ align
    def _find_gap(self, after: float, total_dur: float, first_span=2200.0, max_span=8800.0):
        # max_span must be at least first_span*2 or the "while span <=
        # max_span" loop below runs exactly once no matter how many times
        # span doubles -- silently disabling the expanding-window retry
        # this is meant to provide and sending every chapter longer than
        # first_span straight to the prev_actual+900s fallback guess.
        span = first_span
        while span <= max_span:
            clip_start = after + 3.0
            clip_dur = min(span, total_dur - clip_start)
            if clip_dur <= 0:
                return None, "eof"
            intervals = detect_silences(self.config.mp3, clip_start, clip_dur,
                                         noise_db=-30, min_silence=self.config.min_gap)
            if intervals:
                abs_s, abs_e = intervals[0]  # detect_silences already returns absolute offsets
                mid = (abs_s + abs_e) / 2
                comp = detect_silences(self.config.mp3, abs_e, 10.0, noise_db=-30, min_silence=1.5)
                return mid, ("high" if comp else "low")
            span *= 2
        return None, "fallback"

    def stage_align(self, budget: float, cb: ProgressCB = None) -> bool:
        chapters = self.state["epub"]["chapters"]
        n_chapters = len(chapters)
        total_dur = self.state["mp3_duration"]
        align = self.state["align"]

        if "1" not in align["boundaries"]:
            if self.config.detect_intro:
                # Only trust this as "end of intro" if it's early (a real
                # intro/credits segment is short) -- otherwise a book with
                # no intro at all would have chapter 1's own internal
                # transition gap mistaken for "end of intro", silently
                # shifting every chapter label by one for the whole book.
                actual, _conf = self._find_gap(0.0, total_dur, first_span=600.0, max_span=600.0)
                if actual is None or actual > self.config.intro_max_len:
                    actual = 0.0
            else:
                actual = 0.0
            align["boundaries"]["1"] = actual
            align["next"] = 2
            self._save()

        t0 = time.monotonic()
        while align["next"] <= n_chapters and (time.monotonic() - t0) < budget:
            i = align["next"]
            prev_actual = align["boundaries"][str(i - 1)]
            actual, confidence = self._find_gap(prev_actual, total_dur)
            if actual is None:
                actual = prev_actual + 900.0
                align["fallback"].append(i)
            elif confidence == "low":
                align["low_confidence"].append(i)
            align["boundaries"][str(i)] = actual
            align["next"] = i + 1
            self._save()

        done = align["next"] > n_chapters
        if done and "outro_start" not in align:
            outro_start = None
            if self.config.detect_outro:
                last_actual = align["boundaries"][str(n_chapters)]
                candidate, _conf = self._find_gap(
                    last_actual, total_dur,
                    first_span=min(self.config.outro_max_search, 900.0),
                    max_span=self.config.outro_max_search,
                )
                # Only trust it as "start of outro" if what's left after it
                # is short -- a real outro (end credits) is brief. A long
                # remainder means we probably just found an ordinary pause
                # inside the last chapter's own content.
                if candidate is not None and (total_dur - candidate) <= self.config.outro_max_tail:
                    outro_start = candidate
            align["outro_start"] = outro_start  # None -> no separate outro segment
            self._save()

        self._log(
            f"align: {align['next'] - 1}/{n_chapters} chapters "
            f"(low_confidence={len(align['low_confidence'])} fallback={len(align['fallback'])})",
            cb,
        )

        if done and "outro_start" in align:
            self.state["stage"] = "encode"
            self._save()
            self._build_segments(cb)
            return True
        return False

    def _build_segments(self, cb: ProgressCB = None) -> None:
        chapters = self.state["epub"]["chapters"]
        align = self.state["align"]
        total_dur = self.state["mp3_duration"]
        segments = []
        idx = 0
        if align["boundaries"]["1"] > 5.0:  # meaningful intro exists
            segments.append({"n": idx, "title": self.config.intro_title, "start": 0.0,
                              "end": round(align["boundaries"]["1"], 3)})
            idx += 1
        for i, c in enumerate(chapters, start=1):
            start = align["boundaries"][str(i)]
            if i < len(chapters):
                end = align["boundaries"][str(i + 1)]
            else:
                end = align.get("outro_start") or total_dur
            segments.append({"n": idx, "title": c["title"],
                              "start": round(start, 3), "end": round(end, 3)})
            idx += 1
        if align.get("outro_start") and (total_dur - align["outro_start"]) > 2.0:
            segments.append({"n": idx, "title": self.config.outro_title,
                              "start": round(align["outro_start"], 3), "end": round(total_dur, 3)})
        for s in segments:
            s["duration"] = round(s["end"] - s["start"], 3)
        self.state["segments"] = segments
        self.state["encode_progress"] = []
        self._save()

        # sanity check against word counts
        wc_by_title = {c["title"]: c["word_count"] for c in chapters}
        total_words = sum(wc_by_title.values())
        speech = sum(s["duration"] for s in segments if s["title"] in wc_by_title)
        rate = speech / total_words if total_words else 0
        flagged = []
        for s in segments:
            wc = wc_by_title.get(s["title"])
            if wc is None:
                continue
            exp = wc * rate
            if exp > 0 and abs(s["duration"] - exp) / exp > 0.30:
                flagged.append({"n": s["n"], "title": s["title"],
                                 "actual_duration_s": round(s["duration"], 1),
                                 "expected_duration_s": round(exp, 1)})
        self.state["word_count_warnings"] = flagged
        if flagged:
            self._log(
                f"WARNING: {len(flagged)} segment(s) deviate >30% from word-count estimate "
                "(possible merged/skipped chapter) -- review before trusting output",
                cb,
            )

    # ----------------------------------------------------------------- encode
    def stage_encode(self, budget: float, cb: ProgressCB = None) -> bool:
        seg_dir = self.work_dir / "segments"
        seg_dir.mkdir(exist_ok=True)
        segments = self.state["segments"]
        done = set(self.state["encode_progress"])
        pending = [s for s in segments if s["n"] not in done]

        workers = max(1, min(_MAX_ENCODE_WORKERS, os.cpu_count() or 1, len(pending)))
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            in_flight: dict = {}
            next_idx = 0
            while next_idx < len(pending) or in_flight:
                while (next_idx < len(pending) and len(in_flight) < workers
                       and (time.monotonic() - t0) < budget):
                    seg = pending[next_idx]
                    next_idx += 1
                    out_path = seg_dir / safe_name(seg["n"], seg["title"])
                    future = pool.submit(encode_one, self.config.mp3, seg["start"], seg["end"],
                                          out_path, bitrate=self.config.bitrate)
                    in_flight[future] = seg
                if not in_flight:
                    break
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    seg = in_flight.pop(future)
                    future.result()  # re-raise if this segment's encode failed
                    done.add(seg["n"])
                self.state["encode_progress"] = sorted(done)
                self._save()

        self._log(f"encode: {len(done)}/{len(segments)} segments", cb)
        if len(done) >= len(segments):
            self.state["stage"] = "assemble"
            self._save()
            return True
        return False

    # --------------------------------------------------------------- assemble
    def stage_assemble(self, budget: float, cb: ProgressCB = None) -> bool:
        seg_dir = self.work_dir / "segments"
        segments = self.state["segments"]
        meta = self.state["epub"]["meta"]
        cover = self.state["epub"]["cover"]

        list_path = self.work_dir / "concat_list.txt"
        lines = [f"file '{(seg_dir / safe_name(s['n'], s['title'])).resolve().as_posix()}'"
                 for s in segments]
        list_path.write_text("\n".join(lines) + "\n")

        tmp_dir = self.work_dir / "assemble_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        concat_audio = tmp_dir / "concat_audio.m4a"

        self._log("concatenating segments (this can take a bit for long books)...", cb)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c", "copy", str(concat_audio)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise PipelineError(f"ffmpeg failed concatenating segments: {proc.stderr.strip()[-500:]}")

        durs = [ffprobe_duration(seg_dir / safe_name(s["n"], s["title"])) for s in segments]

        meta_path = tmp_dir / "chapters.ffmeta"
        lines = [";FFMETADATA1", f"title={meta['title']}", f"artist={meta['author']}",
                 f"album={meta['title']}", f"album_artist={meta['author']}", "genre=Audiobook"]
        if meta.get("year"):
            lines.append(f"date={meta['year']}")
        if meta.get("series"):
            lines.append(f"show={meta['series']}")
            lines.append(f"episode_id={meta['series_index']}")
        cum = 0.0
        for s, d in zip(segments, durs):
            title = (s["title"].replace("\\", "\\\\").replace("=", "\\=")
                     .replace(";", "\\;").replace("#", "\\#"))
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(cum * 1000)}",
                      f"END={int((cum + d) * 1000)}", f"title={title}"]
            cum += d
        meta_path.write_text("\n".join(lines) + "\n")

        final_path = tmp_dir / f"{slugify(meta['title'])}.m4b"
        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", str(concat_audio)]
        if cover:
            cmd += ["-i", cover]
        cmd += ["-i", str(meta_path)]
        if cover:
            cmd += ["-map", "0:a", "-map", "1:v", "-map_metadata", "2",
                    "-c:a", "copy", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        else:
            cmd += ["-map", "0:a", "-map_metadata", "1", "-c:a", "copy"]
        cmd += ["-f", "mp4", str(final_path)]
        self._log("muxing final m4b...", cb)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise PipelineError(f"ffmpeg failed muxing final m4b: {proc.stderr.strip()[-500:]}")

        self.state["assembled_path"] = str(final_path)
        self.state["stage"] = "deliver"
        self.state["deliver_bytes_done"] = 0
        self._save()
        self._log(f"assembled: {final_path} ({final_path.stat().st_size / 1e6:.0f} MB)", cb)
        return True

    # ---------------------------------------------------------------- deliver
    def stage_deliver(self, budget: float, cb: ProgressCB = None, chunk_mb: int = 250) -> bool:
        src = Path(self.state["assembled_path"])
        dst = self.config.out_dir / src.name
        self.config.out_dir.mkdir(parents=True, exist_ok=True)
        total = src.stat().st_size
        done = self.state.get("deliver_bytes_done", 0)

        chunk = chunk_mb * 1024 * 1024
        with open(src, "rb") as fsrc:
            fsrc.seek(done)
            mode = "r+b" if dst.exists() and done > 0 else "wb"
            with open(dst, mode) as fdst:
                fdst.seek(done)
                t0 = time.monotonic()
                while done < total and (time.monotonic() - t0) < budget:
                    buf = fsrc.read(chunk)
                    if not buf:
                        break
                    fdst.write(buf)
                    done += len(buf)
                    self.state["deliver_bytes_done"] = done
                    self._save()

        self._log(f"deliver: {done / 1e6:.0f}/{total / 1e6:.0f} MB -> {dst}", cb)
        if done >= total:
            self.state["stage"] = "done"
            self.state["final_output"] = str(dst)
            self._save()
            # scratch is no longer needed once the final file has been
            # delivered into out_dir; segments/tmp can be sizeable
            shutil.rmtree(self.work_dir / "segments", ignore_errors=True)
            shutil.rmtree(self.work_dir / "assemble_tmp", ignore_errors=True)
            self._log(f"DONE: {dst}", cb)
            return True
        return False

    # ------------------------------------------------------------------- run
    def run(self, budget: float, cb: ProgressCB = None) -> dict:
        """Do up to `budget` seconds of work on whatever stage the job is
        currently in, checkpoint, and return the current status (see
        summarize()). Safe to call again immediately -- if the job is
        already done this is a no-op that just returns the final status."""
        stage = self.state["stage"]
        method = {
            "extract": self.stage_extract,
            "align": self.stage_align,
            "encode": self.stage_encode,
            "assemble": self.stage_assemble,
            "deliver": self.stage_deliver,
        }.get(stage)
        if method is not None:
            method(budget, cb)
        return self.summarize()

    # ------------------------------------------------------------- inspect
    def _overall_progress(self) -> float:
        """0.0-1.0 fraction of the *whole job* done so far, combining every
        completed stage's fixed weight with how far along the current
        stage's own countable unit of work (chapters aligned, segments
        encoded, bytes delivered) is. Unlike a per-call elapsed-time ratio,
        this is comparable across separate continue_conversion/
        run_until_done calls -- e.g. going from 0.30 to 0.34 always means
        "4% of the whole job," regardless of how many calls it took."""
        stage = self.state["stage"]
        if stage == "done":
            return 1.0
        stages_before = STAGES[:STAGES.index(stage)]
        completed = sum(_STAGE_WEIGHTS[s] for s in stages_before)

        fraction_within = 0.0
        if stage == "align":
            align = self.state.get("align")
            epub = self.state.get("epub")
            if align and epub and epub["chapters"]:
                fraction_within = max(0, align["next"] - 1) / len(epub["chapters"])
        elif stage == "encode":
            segments = self.state.get("segments")
            if segments:
                fraction_within = len(self.state.get("encode_progress", [])) / len(segments)
        elif stage == "deliver" and self.state.get("assembled_path"):
            total = Path(self.state["assembled_path"]).stat().st_size
            if total:
                fraction_within = self.state.get("deliver_bytes_done", 0) / total

        return round(completed + _STAGE_WEIGHTS[stage] * fraction_within, 4)

    def summarize(self) -> dict:
        """Read-only status snapshot -- does no work, safe to call anytime."""
        epub = self.state.get("epub")
        align = self.state.get("align")
        segments = self.state.get("segments")
        overall_progress = self._overall_progress()
        out = {
            "stage": self.state["stage"],
            "done": self.state["stage"] == "done",
            "status_line": self.state.get("status_line", ""),
            "overall_progress": overall_progress,
            "overall_progress_pct": round(overall_progress * 100, 1),
            "book": {
                "title": epub["meta"]["title"], "author": epub["meta"]["author"],
                "series": epub["meta"]["series"], "series_index": epub["meta"]["series_index"],
                "chapter_count": len(epub["chapters"]),
                "cover_found": bool(epub["cover"]),
            } if epub else None,
        }
        if align:
            n_chapters = len(epub["chapters"])
            out["alignment"] = {
                "chapters_aligned": max(0, align["next"] - 1),
                "chapters_total": n_chapters,
                "low_confidence_chapters": align["low_confidence"],
                "fallback_chapters": align["fallback"],
                "manual_overrides": align.get("manual_overrides", []),
                "outro_detected": bool(align.get("outro_start")),
            }
        if segments:
            out["encoding"] = {
                "segments_encoded": len(self.state.get("encode_progress", [])),
                "segments_total": len(segments),
            }
        if self.state.get("word_count_warnings"):
            out["word_count_warnings"] = self.state["word_count_warnings"]
        if self.state.get("assembled_path"):
            out["assembled_path"] = self.state["assembled_path"]
        if self.state.get("stage") == "deliver":
            total = Path(self.state["assembled_path"]).stat().st_size
            out["delivery"] = {
                "bytes_done": self.state.get("deliver_bytes_done", 0),
                "bytes_total": total,
            }
        if self.state.get("final_output"):
            out["final_output"] = self.state["final_output"]
        out["recent_log"] = self.state.get("log", [])[-10:]
        return out

    # --------------------------------------------------------------- repair
    def patch_boundary(self, chapter_index: int, new_start_seconds: float) -> dict:
        """Manually override chapter `chapter_index`'s detected start time,
        implementing the skill's documented fix for a flagged or fallback
        chapter: patch align.boundaries, invalidate every stage derived
        from it (segments/encode/assemble/deliver), and drop the job back
        to the "encode" stage (via a fresh _build_segments()) so the next
        continue_conversion/run_until_done call regenerates everything
        downstream of the fix. Chapters before/after the patched one are
        untouched.
        """
        align = self.state.get("align")
        if align is None:
            raise PipelineError("This job hasn't reached the alignment stage yet.")
        n_chapters = len(self.state["epub"]["chapters"])
        if not (1 <= chapter_index <= n_chapters):
            raise PipelineError(f"chapter_index must be between 1 and {n_chapters}, got {chapter_index}.")
        if str(chapter_index) not in align["boundaries"]:
            raise PipelineError(
                f"Chapter {chapter_index} hasn't been aligned yet (alignment has reached "
                f"chapter {max(0, align['next'] - 1)}/{n_chapters}). Wait until it's been "
                "processed before patching it."
            )
        if new_start_seconds < 0 or new_start_seconds > self.state["mp3_duration"]:
            raise PipelineError(
                f"new_start_seconds ({new_start_seconds}) is outside the audio file's "
                f"duration (0-{self.state['mp3_duration']:.1f}s)."
            )
        old = align["boundaries"][str(chapter_index)]
        align["boundaries"][str(chapter_index)] = new_start_seconds
        align.setdefault("manual_overrides", [])
        if chapter_index not in align["manual_overrides"]:
            align["manual_overrides"].append(chapter_index)
        align["low_confidence"] = [c for c in align["low_confidence"] if c != chapter_index]
        align["fallback"] = [c for c in align["fallback"] if c != chapter_index]

        invalidated = []
        for key in ("segments", "encode_progress", "assembled_path", "deliver_bytes_done", "final_output"):
            if key in self.state:
                del self.state[key]
                invalidated.append(key)
        if self.state["stage"] != "align":
            self.state["stage"] = "encode"

        self._save()
        if align["next"] > n_chapters and "outro_start" in align:
            # alignment already finished for the whole book -- rebuild
            # segments now so the job is immediately ready to re-encode.
            self._build_segments()

        self._log(
            f"patched chapter {chapter_index} boundary: {old:.2f}s -> {new_start_seconds:.2f}s "
            f"(invalidated: {', '.join(invalidated) or 'nothing downstream yet'})"
        )
        return {
            "chapter_index": chapter_index,
            "old_start_seconds": round(old, 3),
            "new_start_seconds": round(new_start_seconds, 3),
            "invalidated": invalidated,
            "stage_now": self.state["stage"],
        }

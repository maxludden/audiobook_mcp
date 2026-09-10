"""
Persistent job registry.

Each conversion job's real state of record is its own `state.json` inside
`<out_dir>/.state/<job_id>/` (see pipeline.py) -- that file alone is
enough to rehydrate a Pipeline. The registry just answers "given a job_id,
which work_dir is that?" so tools don't need the caller to keep resupplying
out_dir/mp3/epub on every call, and so `list_conversions` doesn't have to
guess where to look on disk.

The registry itself lives at $AUDIOBOOK_MCP_HOME/registry.json (default
~/.audiobook_mcp/registry.json) and persists across server restarts. It's
deliberately tiny (job_id -> work_dir + a few display fields) precisely so
it's safe to rebuild by re-scanning known out_dirs if it's ever lost --
see `Registry.reconcile()`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_HOME = Path(os.environ.get("AUDIOBOOK_MCP_HOME", Path.home() / ".audiobook_mcp"))
_REGISTRY_PATH = _HOME / "registry.json"

_lock = threading.Lock()


def _slugify(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "job"


class RegistryError(RuntimeError):
    pass


class Registry:
    """Thread-safe (single-process) JSON-file-backed job registry."""

    def __init__(self, path: Path = _REGISTRY_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"jobs": {}}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"jobs": {}}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    def create_job(self, *, mp3: Path, epub: Path, out_dir: Path, title_hint: str) -> tuple[str, Path]:
        """Register a new job (or return the existing one if an identical
        mp3+epub+out_dir job already exists) and return (job_id, work_dir).
        """
        with _lock:
            data = self._read()
            for jid, rec in data["jobs"].items():
                if (rec["mp3"] == str(mp3) and rec["epub"] == str(epub)
                        and rec["out_dir"] == str(out_dir)):
                    return jid, Path(rec["work_dir"])

            base_slug = _slugify(title_hint or epub.stem)
            job_id = base_slug
            existing_ids = set(data["jobs"].keys())
            if job_id in existing_ids:
                job_id = f"{base_slug}-{uuid.uuid4().hex[:6]}"

            work_dir = out_dir / ".state" / job_id
            data["jobs"][job_id] = {
                "mp3": str(mp3), "epub": str(epub), "out_dir": str(out_dir),
                "work_dir": str(work_dir), "created_at": time.time(),
            }
            self._write(data)
            return job_id, work_dir

    def get(self, job_id: str) -> dict:
        with _lock:
            data = self._read()
        rec = data["jobs"].get(job_id)
        if rec is None:
            raise RegistryError(
                f"No job with id {job_id!r} is registered. Use audiobook_list_conversions "
                "to see known jobs, or audiobook_start_conversion to create a new one."
            )
        return rec

    def list(self, out_dir: Optional[Path] = None, limit: int = 20, offset: int = 0) -> dict:
        with _lock:
            data = self._read()
        items = [
            {"job_id": jid, **rec} for jid, rec in data["jobs"].items()
            if out_dir is None or rec["out_dir"] == str(out_dir)
        ]
        items.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        total = len(items)
        page = items[offset:offset + limit]
        return {
            "total": total, "count": len(page), "offset": offset,
            "jobs": page,
            "has_more": total > offset + len(page),
            "next_offset": offset + len(page) if total > offset + len(page) else None,
        }

    def delete(self, job_id: str) -> dict:
        with _lock:
            data = self._read()
            rec = data["jobs"].pop(job_id, None)
            if rec is None:
                raise RegistryError(f"No job with id {job_id!r} is registered.")
            self._write(data)
            return rec

"""Where an uploaded student list waits while the Admin works through the import screen.

The import is four steps — upload, map the columns, read the validation preview, commit — and an
HTML file input cannot be refilled between them, so the uploaded file is kept on the server under a
random batch id until the Admin either commits it or walks away.

WHAT IS KEPT AND FOR HOW LONG
  * `<staging root>/<batch id>/upload.<ext>`  the bytes exactly as they were uploaded
  * `<staging root>/<batch id>/batch.json`    the file name, who uploaded it, the chosen column
                                              mapping, the photo folder and, afterwards, the summary
A batch older than STALE_AFTER_HOURS is deleted the next time anything looks at the staging area, so
the university's student list never lingers on a venue laptop for longer than the working day.

The batch id is 32 random hex characters from `secrets`, so a batch cannot be guessed. Every route
that uses one is Admin-only regardless.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import secrets
import shutil
import tempfile
import time
from typing import Any, Optional

logger = logging.getLogger("backend.admin")

STALE_AFTER_HOURS = 12
_ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xls", ".txt"}


class BatchNotFound(Exception):
    """No such batch (or it has been cleaned up). The caller turns this into a plain 404."""


@dataclasses.dataclass
class Batch:
    batch_id: str
    folder: pathlib.Path
    meta: dict

    @property
    def upload_path(self) -> pathlib.Path:
        return self.folder / self.meta["stored_as"]

    @property
    def filename(self) -> str:
        return self.meta.get("filename") or "upload.csv"

    @property
    def mapping(self) -> Optional[dict]:
        return self.meta.get("mapping")

    @property
    def photo_dir(self) -> Optional[str]:
        return self.meta.get("photo_dir") or None

    @property
    def summary(self) -> Optional[dict]:
        return self.meta.get("summary")

    @property
    def committed(self) -> bool:
        return self.meta.get("summary") is not None

    def read_bytes(self) -> bytes:
        return self.upload_path.read_bytes()


def root(settings) -> pathlib.Path:
    """The staging area for this server. Configurable so a venue can put it on the same encrypted
    volume as the rest of the student data."""
    configured = getattr(settings, "import_staging_dir", None)
    folder = pathlib.Path(configured) if configured else pathlib.Path(tempfile.gettempdir()) / "convocation-imports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe_suffix(filename: str) -> str:
    suffix = pathlib.Path(filename or "").suffix.lower()
    return suffix if suffix in _ALLOWED_SUFFIXES else ".csv"


def sweep(settings) -> int:
    """Delete batches older than STALE_AFTER_HOURS. Returns how many went."""
    cutoff = time.time() - STALE_AFTER_HOURS * 3600
    removed = 0
    for folder in root(settings).iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:  # a batch another worker is removing right now: not our problem
            continue
    if removed:
        logger.info("cleared %s stale import batch(es)", removed)
    return removed


def create(settings, *, content: bytes, filename: str, uploaded_by: Any, photo_dir: Optional[str] = None) -> Batch:
    sweep(settings)
    batch_id = secrets.token_hex(16)
    folder = root(settings) / batch_id
    folder.mkdir(parents=True)
    stored_as = f"upload{_safe_suffix(filename)}"
    (folder / stored_as).write_bytes(content)
    meta = {
        "batch_id": batch_id,
        "filename": filename or stored_as,
        "stored_as": stored_as,
        "uploaded_by": str(uploaded_by) if uploaded_by else None,
        "uploaded_at": time.time(),
        "photo_dir": (photo_dir or "").strip() or None,
        "mapping": None,
        "summary": None,
    }
    _write_meta(folder, meta)
    return Batch(batch_id=batch_id, folder=folder, meta=meta)


def load(settings, batch_id: str) -> Batch:
    if not (isinstance(batch_id, str) and len(batch_id) == 32 and all(c in "0123456789abcdef" for c in batch_id)):
        raise BatchNotFound(batch_id)
    folder = root(settings) / batch_id
    meta_path = folder / "batch.json"
    if not meta_path.is_file():
        raise BatchNotFound(batch_id)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    batch = Batch(batch_id=batch_id, folder=folder, meta=meta)
    if not batch.upload_path.is_file():
        raise BatchNotFound(batch_id)
    return batch


def update(batch: Batch, **fields) -> Batch:
    batch.meta.update(fields)
    _write_meta(batch.folder, batch.meta)
    return batch


def _write_meta(folder: pathlib.Path, meta: dict) -> None:
    (folder / "batch.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

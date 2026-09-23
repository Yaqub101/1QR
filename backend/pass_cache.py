"""backend/pass_cache.py — Safe artifact caching for generated pass PDFs.

Reuses existing generated pass PDFs when underlying student data, photos, QR tokens,
event title, and pass template have not changed.
Provides single-flight locking to prevent concurrent heavy ReportLab rendering tasks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import secrets
import tempfile
import threading
from typing import Any, Callable, Optional, Sequence

from backend.passes import PassData, PassWarning, RenderResult, render_sheets

logger = logging.getLogger("backend.pass_cache")

# Bump this string whenever pass layout or styling in backend/passes.py changes
PASS_TEMPLATE_VERSION = "pass_v1_300dpi"

DEFAULT_CACHE_DIR = pathlib.Path(tempfile.gettempdir()) / "convocation-pass-cache"


def compute_pass_fingerprint(
    event_name: str,
    rows: Sequence[dict],
    extra: Optional[dict] = None,
    template_version: str = PASS_TEMPLATE_VERSION,
) -> str:
    """Cryptographic SHA-256 fingerprint representing the exact source inputs to a pass PDF.

    Any change to student details (name, PRN, programme, sequence_no, photo_path),
    QR tokens, event title, filters, or pass template invalidates the fingerprint.
    """
    hasher = hashlib.sha256()
    hasher.update(template_version.encode("utf-8"))
    hasher.update(b"|event:")
    hasher.update(event_name.strip().encode("utf-8"))
    if extra:
        hasher.update(b"|extra:")
        hasher.update(json.dumps(extra, sort_keys=True, default=str).encode("utf-8"))

    # Each row contributes its identity, student fields, photo path, and QR token
    for r in rows:
        hasher.update(b"|s:")
        line = (
            f"{r.get('id')}:{r.get('prn')}:{r.get('name')}:"
            f"{r.get('programme')}:{r.get('photo_path')}:"
            f"{r.get('sequence_no')}:{r.get('token')}"
        )
        hasher.update(line.encode("utf-8"))

    return hasher.hexdigest()


class PassArtifactCache:
    """Atomic, disk-backed cache for rendered pass PDFs with in-process single-flight locking."""

    def __init__(self, cache_dir: Optional[pathlib.Path] = None):
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._flight_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _ensure_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _paths(self, fingerprint: str) -> tuple[pathlib.Path, pathlib.Path]:
        return (
            self.cache_dir / f"{fingerprint}.pdf",
            self.cache_dir / f"{fingerprint}.meta.json",
        )

    def get(self, fingerprint: str) -> Optional[RenderResult]:
        """Retrieve a cached PDF artifact if it exists and is intact."""
        pdf_path, meta_path = self._paths(fingerprint)
        if not pdf_path.is_file() or not meta_path.is_file():
            return None
        try:
            pdf_bytes = pdf_path.read_bytes()
            if not pdf_bytes.startswith(b"%PDF"):
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            warnings = [
                PassWarning(
                    prn=w.get("prn", ""),
                    code=w.get("code", ""),
                    detail=w.get("detail", ""),
                )
                for w in meta.get("warnings", [])
            ]
            count = meta.get("count", len(warnings))
            logger.info("Pass cache hit for fingerprint %s (%d passes, %d bytes)",
                        fingerprint[:12], count, len(pdf_bytes))
            return RenderResult(pdf=pdf_bytes, count=count, warnings=warnings)
        except Exception as exc:
            logger.warning("Could not read cached pass artifact %s: %s", fingerprint[:12], exc)
            return None

    def put(self, fingerprint: str, result: RenderResult) -> None:
        """Atomically persist a generated PDF artifact and its metadata."""
        self._ensure_dir()
        pdf_path, meta_path = self._paths(fingerprint)
        token = secrets.token_hex(6)
        tmp_pdf = self.cache_dir / f".{fingerprint}.{token}.pdf.tmp"
        tmp_meta = self.cache_dir / f".{fingerprint}.{token}.meta.tmp"

        meta_content = {
            "fingerprint": fingerprint,
            "count": result.count,
            "warning_count": len(result.warnings),
            "warnings": [
                {"prn": w.prn, "code": w.code, "detail": getattr(w, "detail", "")}
                for w in result.warnings
            ],
        }

        try:
            tmp_pdf.write_bytes(result.pdf)
            tmp_meta.write_text(json.dumps(meta_content), encoding="utf-8")
            os.replace(tmp_pdf, pdf_path)
            os.replace(tmp_meta, meta_path)
            logger.info("Cached pass artifact %s (%d passes, %d bytes)",
                        fingerprint[:12], result.count, len(result.pdf))
        except Exception as exc:
            logger.warning("Failed to write pass artifact %s: %s", fingerprint[:12], exc)
            tmp_pdf.unlink(missing_ok=True)
            tmp_meta.unlink(missing_ok=True)

    def invalidate(self, fingerprint: Optional[str] = None) -> int:
        """Invalidate a specific artifact or the entire cache."""
        if not self.cache_dir.is_dir():
            return 0
        removed = 0
        if fingerprint:
            pdf_path, meta_path = self._paths(fingerprint)
            for p in (pdf_path, meta_path):
                if p.is_file():
                    p.unlink(missing_ok=True)
                    removed += 1
            return removed

        for p in self.cache_dir.iterdir():
            if p.is_file() and (p.suffix in (".pdf", ".json", ".tmp") or ".tmp" in p.name):
                try:
                    p.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
        return removed

    def get_or_render(
        self,
        event_name: str,
        rows: Sequence[dict],
        data: Sequence[PassData],
        extra: Optional[dict] = None,
        renderer: Callable[[Sequence[PassData], str], RenderResult] = render_sheets,
    ) -> RenderResult:
        """Single-flight cached rendering: returns cached artifact if present, or renders once."""
        fingerprint = compute_pass_fingerprint(event_name, rows, extra=extra)

        # 1. Fast path: already cached
        cached = self.get(fingerprint)
        if cached is not None:
            return cached

        # 2. Synchronize concurrent renders for the same fingerprint
        with self._meta_lock:
            if fingerprint not in self._flight_locks:
                self._flight_locks[fingerprint] = threading.Lock()
            flight_lock = self._flight_locks[fingerprint]

        with flight_lock:
            # Re-check cache after acquiring lock in case another thread just completed it
            cached = self.get(fingerprint)
            if cached is not None:
                return cached

            logger.info("Rendering %d passes for event '%s' (cache miss: %s)",
                        len(data), event_name, fingerprint[:12])
            result = renderer(data, event_name)
            self.put(fingerprint, result)
            return result


# Process-wide default cache instance
default_cache = PassArtifactCache()


def get_or_render_sheets(
    event_name: str,
    rows: Sequence[dict],
    data: Sequence[PassData],
    extra: Optional[dict] = None,
    cache: Optional[PassArtifactCache] = None,
) -> RenderResult:
    """Helper used by Admin routes to fetch or render sheets with safe artifact caching."""
    active_cache = cache or default_cache
    return active_cache.get_or_render(event_name, rows, data, extra=extra)

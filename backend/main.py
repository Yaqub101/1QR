import io
import json
import pathlib
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Connection

from backend.config import Settings, get_settings
from backend.logging_config import setup_logging
from backend import database
from backend.importer import parse_file, detect_column_mapping, validate_import, commit_import
from backend.photos import link_photos_by_prn
from backend.snapshot import freeze_display_data
from backend.master_pack import export_master_pack, import_master_pack

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)

    engine = database.get_engine(settings.database_url)

    app = FastAPI(
        title=f"Convocation System ({settings.mode.capitalize()} Mode)",
        version="0.1.0",
    )

    app.state.settings = settings
    app.state.engine = engine

    # ── Static files ─────────────────────────────────────────────────────────
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get("/health")
    def health_check() -> Dict[str, Any]:
        is_db_up = database.check_db_health(engine=engine)
        return {
            "mode": settings.mode,
            "venue": settings.venue_id,
            "db": "up" if is_db_up else "down",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Admin: import ─────────────────────────────────────────────────────────
    @app.post("/admin/import/preview")
    async def import_preview(
        file: UploadFile = File(...),
        column_mapping: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        """Parse uploaded CSV/XLSX and return a validation preview."""
        content = await file.read()
        try:
            cols, rows = parse_file(io.BytesIO(content), file.filename or "upload.csv")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {exc}")

        mapping = json.loads(column_mapping) if column_mapping else detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)

        return {
            "columns": cols,
            "mapping": mapping,
            "to_create": len(preview.to_create),
            "to_skip": len(preview.to_skip),
            "flagged_duplicates": [
                {"row": d.row, "prn": d.prn, "type": d.type, "message": d.message}
                for d in preview.flagged_duplicates
            ],
            "errors": [
                {"row": e.row, "field": e.field, "message": e.message}
                for e in preview.errors
            ],
            "is_valid": preview.is_valid,
        }

    @app.post("/admin/import/commit")
    async def import_commit(
        file: UploadFile = File(...),
        column_mapping: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        """Validate and transactionally commit the uploaded file."""
        content = await file.read()
        try:
            cols, rows = parse_file(io.BytesIO(content), file.filename or "upload.csv")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {exc}")

        mapping = json.loads(column_mapping) if column_mapping else detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            if not preview.is_valid:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "Validation failed — no rows written",
                        "errors": [
                            {"row": e.row, "field": e.field, "message": e.message}
                            for e in preview.errors
                        ],
                    },
                )
            summary = commit_import(preview, conn)

        return {
            "read": summary.read,
            "created": summary.created,
            "updated": summary.updated,
            "skipped": summary.skipped,
            "errors": summary.errors,
        }

    # ── Admin: photos ─────────────────────────────────────────────────────────
    @app.post("/admin/photos/link")
    async def photos_link(
        photo_dir: str = Form(...),
    ) -> Dict[str, Any]:
        """Link photos from a server-side directory to students by PRN."""
        p = pathlib.Path(photo_dir)
        if not p.is_dir():
            raise HTTPException(status_code=400, detail=f"Directory not found: {photo_dir}")
        with engine.connect() as conn:
            report = link_photos_by_prn(p, conn)
        return {
            "matched_count": report.matched_count,
            "unmatched_photos": report.unmatched_photos,
            "unmatched_students": report.unmatched_students,
        }

    # ── Admin: snapshot ───────────────────────────────────────────────────────
    @app.post("/admin/snapshot/freeze")
    def snapshot_freeze() -> Dict[str, Any]:
        """Freeze display_snapshot from current student master records."""
        with engine.connect() as conn:
            summary = freeze_display_data(conn)
        return {"frozen_count": summary.frozen_count}

    # ── Admin: master pack ────────────────────────────────────────────────────
    @app.get("/admin/master-pack/export")
    def master_pack_export(photos_dir: Optional[str] = None) -> FileResponse:
        """Export master pack ZIP (students, snapshots, tokens, photos)."""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = pathlib.Path(tmp.name)

        with engine.connect() as conn:
            export_master_pack(
                tmp_path,
                conn,
                photos_dir=pathlib.Path(photos_dir) if photos_dir else None,
            )

        return FileResponse(
            path=str(tmp_path),
            filename="master_pack.zip",
            media_type="application/zip",
        )

    @app.post("/admin/master-pack/import")
    async def master_pack_import(
        file: UploadFile = File(...),
        photos_target: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        """Import a master pack ZIP into this venue database."""
        content = await file.read()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(content)
            tmp_path = pathlib.Path(tmp.name)

        with engine.connect() as conn:
            details = import_master_pack(
                tmp_path,
                conn,
                photos_target_dir=pathlib.Path(photos_target) if photos_target else None,
            )

        tmp_path.unlink(missing_ok=True)
        return details

    return app


def get_app() -> FastAPI:
    return create_app()


try:
    app = get_app()
except Exception:
    app = None

import io
import json
import logging
import pathlib
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Connection
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.audit import write_audit
from backend.config import Settings, get_settings
from backend.logging_config import setup_logging
from backend import database, photo_storage
from backend.importer import commit_import, read_and_validate
from backend.photos import link_photos_by_prn
from backend.snapshot import freeze_display_data
from backend.master_pack import export_master_pack, import_master_pack
from backend.engine.routes import router as engine_router
from backend.stage.routes import router as stage_router
from backend.security.deps import require_admin
from backend.security.sessions import Principal
from backend.admin.routes import router as admin_console_router
from backend.web_admin import router as admin_router
from backend.web_import import router as import_router
from backend.web_auth import router as auth_router
from backend.web_system import router as system_router
from backend.admin import reset as reset_svc

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)

    engine = database.get_engine(settings.database_url)

    app = FastAPI(title="Convocation System", version="0.1.0")

    app.state.settings = settings
    app.state.engine = engine
    # Where student photos live (local folder or Cloudinary). A misconfiguration stops the server
    # here, with one plain sentence naming the setting, instead of losing photos later.
    try:
        app.state.photo_store = photo_storage.configure(settings)
    except photo_storage.PhotoStorageConfigError as exc:
        logging.getLogger("backend").critical("PHOTO STORAGE IS MISCONFIGURED: %s", exc)
        raise

    @app.middleware("http")
    async def _process_time(request: Request, call_next):
        # Server-side processing time, network excluded: scan-to-confirm must stay well under a second.
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # A browser whose session ended goes back to the sign-in page, not to a raw JSON error.
        if exc.status_code == 401 and request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=303)
        return await http_exception_handler(request, exc)

    def _check_schema_guard():
        try:
            status, missing = database.db_status(engine=engine)
            if status == database.UP and not missing:
                is_match, db_ver, expected_ver = database.check_schema_version(engine=engine)
                if not is_match:
                    logging.getLogger("backend").error(
                        "database schema out of date: db=%s, expected=%s", db_ver, expected_ver
                    )
        except Exception as exc:
            logging.getLogger("backend").warning("Startup schema check could not run: %s", exc)

    _check_schema_guard()

    class CachedStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            query = scope.get("query_string", b"").decode("latin-1")
            if "v=" in query:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    # ── Static files ─────────────────────────────────────────────────────────
    if STATIC_DIR.is_dir():
        app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get("/health")
    def health_check() -> Dict[str, Any]:
        # "up" means this server can actually take a scan: the database is reachable AND the schema
        # the migrations build is there. A database that is merely reachable answers SELECT 1 just as
        # happily when it is completely empty, and a server in that state can do nothing at all, so it
        # reports "not_ready" (run `alembic upgrade head`) rather than a healthy-looking "up".
        status, missing = database.db_status(engine=engine)
        body: Dict[str, Any] = {
            "status": "OK" if status == database.UP else "NOT OK",
            "db": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if missing:
            body["status"] = "NOT OK"
            body["missing_tables"] = missing
            body["detail"] = "the database schema is not set up: run `alembic upgrade head`"
            return body

        if status == database.UP:
            try:
                is_match, db_version, expected_head = database.check_schema_version(engine=engine)
                body["alembic_version"] = db_version
                body["expected_version"] = expected_head
                if not is_match:
                    body["status"] = "NOT OK"
                    body["db"] = database.NOT_READY
                    msg = f"database schema out of date: db={db_version}, expected={expected_head}"
                    body["detail"] = msg
                    body["message"] = msg
            except Exception as e:
                logging.getLogger("backend").error("Schema check failed during health check: %s", e)
                body["status"] = "NOT OK"
                body["detail"] = f"schema check failed: {e}"

        return body

    # ── Sign-in, station screens, admin screens (Phase 5) ────────────────────
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(admin_console_router)  # dashboard, corrections, exceptions, audit, reports (Phases 13/16)
    app.include_router(import_router)         # the Admin import screen (Phase 3)
    app.include_router(system_router)         # Admin → System: the full data reset
    app.include_router(engine_router)  # /scan /search /confirm /photo (Phase 6)
    app.include_router(stage_router)   # /stage/* controller and the public /led/* (Phase 11)

    # ── Admin: import (Admin / Deputy only) ───────────────────────────────────
    # These two are the machine-facing face of the importer; the Admin screen in
    # backend/web_import.py sits in front of the SAME code path (importer.read_and_validate /
    # commit_import), so the two can never drift apart.
    @app.post("/admin/import/preview", dependencies=[Depends(require_admin)])
    async def import_preview(
        file: UploadFile = File(...),
        column_mapping: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        """Parse uploaded CSV/XLSX and return a validation preview."""
        content = await file.read()
        try:
            cols, rows, mapping, preview = read_and_validate(
                engine, content, file.filename or "upload.csv",
                mapping=json.loads(column_mapping) if column_mapping else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {exc}")

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

    @app.post("/admin/import/commit", dependencies=[Depends(require_admin)])
    async def import_commit(
        file: UploadFile = File(...),
        column_mapping: Optional[str] = Form(None),
    ) -> Dict[str, Any]:
        """Validate and transactionally commit the uploaded file."""
        content = await file.read()
        try:
            cols, rows, mapping, preview = read_and_validate(
                engine, content, file.filename or "upload.csv",
                mapping=json.loads(column_mapping) if column_mapping else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot parse file: {exc}")

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
        try:
            with reset_svc.import_lock(engine), engine.connect() as conn:   # never underneath a data reset
                summary = commit_import(preview, conn)
        except reset_svc.Busy as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})

        return {
            "read": summary.read,
            "created": summary.created,
            "updated": summary.updated,
            "skipped": summary.skipped,
            "errors": summary.errors,
        }

    # ── Admin: photos ─────────────────────────────────────────────────────────
    @app.post("/admin/photos/link", dependencies=[Depends(require_admin)])
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
    @app.post("/admin/snapshot/freeze", dependencies=[Depends(require_admin)])
    def snapshot_freeze() -> Dict[str, Any]:
        """Freeze display_snapshot from current student master records."""
        with engine.connect() as conn:
            summary = freeze_display_data(conn)
        return {"frozen_count": summary.frozen_count}

    # ── Admin: master pack ────────────────────────────────────────────────────
    @app.get("/admin/master-pack/export", dependencies=[Depends(require_admin)])
    def master_pack_export(photos_dir: Optional[str] = None, principal: Principal = Depends(require_admin)) -> FileResponse:
        """Export master pack ZIP (students, snapshots, tokens, photos).

        The pack holds every student's details and every QR token, so the export is audited (SYSTEM_SPEC 20) and the
        temporary file is deleted once it has been sent."""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = pathlib.Path(tmp.name)

        try:
            with engine.begin() as conn:
                export_master_pack(
                    tmp_path,
                    conn,
                    photos_dir=pathlib.Path(photos_dir) if photos_dir else None,
                )
                write_audit(conn, "EXPORT_MASTER_PACK", operator_id=principal.user_id,
                            details={"with_photos": bool(photos_dir)})
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        return FileResponse(
            path=str(tmp_path),
            filename="master_pack.zip",
            media_type="application/zip",
            background=BackgroundTask(tmp_path.unlink, missing_ok=True),
        )

    @app.post("/admin/master-pack/import", dependencies=[Depends(require_admin)])
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
except photo_storage.PhotoStorageConfigError:
    raise  # never start a server that would lose photos: the reason is in the log line above
except Exception:
    app = None

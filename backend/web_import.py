"""The Admin import screen (TODO Phase 3): upload -> map the columns -> preview -> commit -> summary.

Phase 3 had a working importer and no way to reach it except curl. This is the screen in front of
it. It writes no import logic of its own: every step goes through `backend.importer`, the same
module the `/admin/import/preview` and `/admin/import/commit` endpoints use, so what an Admin sees
on the preview is exactly what a commit would do.

THE STEP THAT MATTERS is the preview. Nothing is written until the Admin has seen, on one page:
what will be created, what is already on the list and will be left alone, every fatal error with
its row number, and every note worth a second look (no photo, no sequence number, a repeated
sequence number, a row marked inactive). A file with one fatal error offers no commit button at
all, and a commit posted anyway is refused.

Re-running the same file is the normal case, not an edge case: the university sends the list in
halves. A row whose PRN is already on the list is SKIPPED — never updated, never duplicated, and
its QR token is never touched.

PHOTOS COME IN THE SAME FORM. The university's photo ZIP (`N_PROFILE_IMAGE_PRN_No_<id>_Name_...`)
is uploaded next to the student list, streamed to the batch folder in chunks, and checked at once:
a file that is not a readable ZIP is refused before anything is written. On commit the students
go in first, in their own transaction; only after that commit do the photos run, through
`photos.import_photos_from_zip` — the same matcher the CLI uses, with the same spreadsheet used
to turn Enrollment / Roll numbers into PRNs — into the configured photo store (a local folder, or
Cloudinary on Render). If the photo step fails, the students stay imported and the summary says
so. The staged ZIP is deleted as soon as the commit has used it, whichever way that went.
"""
from __future__ import annotations

import logging
import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from backend import import_staging, photo_storage
from backend.admin import reset as reset_svc
from backend.importer import (FIELD_LABELS, REQUIRED_FIELDS, commit_import, detect_column_mapping, excel_engine_for,
                              read_and_validate)
from backend.photos import import_photos_from_zip, inspect_photo_zip, link_photos_by_prn
from backend.security.deps import http_error, require_admin
from backend.security.sessions import Principal
from backend.web import redirect, render

logger = logging.getLogger("backend.admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

SAMPLE_ROWS = 5          # rows of the real file shown on the mapping step
MAX_LISTED = 200         # errors / notes listed on the preview before it says "and N more"


def _batch(request: Request, batch_id: str):
    try:
        return import_staging.load(request.app.state.settings, batch_id)
    except import_staging.BatchNotFound:
        raise http_error(404, "IMPORT_BATCH_NOT_FOUND",
                         "That import has finished or expired. Please upload the file again.")


def _photo_store(request: Request):
    return getattr(request.app.state, "photo_store", None) or photo_storage.current_store()


def _zip_report(report, zip_meta: dict, store) -> dict:
    """The photo half of the summary: the CLI report's categories, in plain lists."""
    return {
        "source": "zip",
        "filename": zip_meta.get("filename"),
        "storage": store.describe(),
        "matched": report.matched_count,
        "unmatched_students": [s["prn"] for s in report.unmatched_students],
        "orphaned": [o["filename"] for o in report.orphaned_photos],
        "duplicate_photo_prns": [{"prn": d["prn"], "filenames": d["filenames"]} for d in report.duplicate_photo_prns],
        "duplicate_student_prns": [d["prn"] for d in report.duplicate_student_prns],
        "malformed": list(report.malformed_filenames),
        "frozen_skipped": [s["prn"] for s in report.frozen_skipped],
        "storage_failed": list(report.storage_failed),
    }


def _preview_for(request: Request, batch):
    """Parse and validate the staged file with the mapping the Admin chose. Writes nothing."""
    return read_and_validate(
        request.app.state.engine, batch.read_bytes(), batch.filename,
        mapping=batch.mapping, photo_dir=batch.photo_dir)


# ══════════════════════════════════════════════════════════════════ step 1: the file
@router.get("/import")
def import_home(request: Request, principal: Principal = Depends(require_admin)):
    return render(request, "admin_import.html", principal=principal)


@router.post("/import/upload")
async def upload(request: Request, file: UploadFile = File(...), photo_dir: str = Form(""),
                 photos_zip: Optional[UploadFile] = File(None),
                 principal: Principal = Depends(require_admin)):
    content = await file.read()   # the student list: a few MB. The photo ZIP is never read whole.
    if not content:
        return redirect("/admin/import", error="That file is empty. Please choose the student list and try again.")

    folder = (photo_dir or "").strip()
    if folder and not pathlib.Path(folder).is_dir():
        return redirect("/admin/import", error=f"There is no folder at {folder} on this server.")

    has_zip = photos_zip is not None and bool(photos_zip.filename)
    if has_zip and folder:
        return redirect("/admin/import", error="Please give either a photo ZIP or a photo folder, not both.")
    if has_zip and _photo_store(request).write_refusal:
        return redirect("/admin/import", error=_photo_store(request).write_refusal)

    batch = import_staging.create(request.app.state.settings, content=content, filename=file.filename or "upload.csv",
                                  uploaded_by=principal.user_id, photo_dir=folder)
    try:
        columns, _, _, _ = _preview_for(request, batch)
    except ValueError as exc:
        logger.info("import upload could not be read: %s", exc)
        import_staging.discard(batch)
        return redirect("/admin/import",
                        error="That file could not be read. Please save it as CSV or XLSX and try again.")
    if not columns:
        import_staging.discard(batch)
        return redirect("/admin/import", error="That file has no columns. Please check it and try again.")

    if has_zip:
        try:
            size = await run_in_threadpool(import_staging.attach_photos_zip, batch, photos_zip.file, photos_zip.filename)
            contents = await run_in_threadpool(inspect_photo_zip, batch.photos_zip_path) if size else None
        except (OSError, ValueError) as exc:
            logger.info("photo ZIP upload refused: %s", exc)
            contents = None
        if not contents:
            import_staging.discard(batch)
            return redirect("/admin/import",
                            error="The photo file is not a readable ZIP, so nothing was imported. Please check it and try again.")
        import_staging.update(batch, photos_zip={**batch.photos_zip, **contents})
    return redirect(f"/admin/import/{batch.batch_id}/columns")


# ══════════════════════════════════════════════════════════════════ step 2: the columns
@router.get("/import/{batch_id}/columns")
def columns_page(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    batch = _batch(request, batch_id)
    columns, file_rows, _, _ = _preview_for(request, batch)
    detected = batch.mapping or detect_column_mapping(columns)
    return render(
        request, "admin_import_columns.html", principal=principal, batch=batch,
        columns=list(enumerate(columns)), chosen={c: detected.get(c, "") for c in columns},
        field_labels=FIELD_LABELS, sample=file_rows[:SAMPLE_ROWS], row_count=len(file_rows),
    )


@router.post("/import/{batch_id}/columns")
async def choose_columns(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    batch = _batch(request, batch_id)
    columns, _, _, _ = _preview_for(request, batch)
    form = await request.form()

    mapping: dict[str, str] = {}
    taken: dict[str, str] = {}
    for index, column in enumerate(columns):
        field = (form.get(f"map__{index}") or "").strip()
        if not field:
            continue
        if field not in FIELD_LABELS:
            return redirect(f"/admin/import/{batch_id}/columns", error="That is not a column the system knows.")
        if field in taken:
            return redirect(f"/admin/import/{batch_id}/columns",
                            error=f"Two columns are both set to {FIELD_LABELS[field]}: “{taken[field]}” and “{column}”. "
                                  f"Please pick one.")
        taken[field] = column
        mapping[column] = field

    missing = [FIELD_LABELS[f] for f in REQUIRED_FIELDS if f not in taken]
    if missing:
        return redirect(f"/admin/import/{batch_id}/columns",
                        error=f"Still to be matched: {', '.join(missing)}.")

    import_staging.update(batch, mapping=mapping)
    return redirect(f"/admin/import/{batch_id}/preview")


# ══════════════════════════════════════════════════════════════════ step 3: the preview
@router.get("/import/{batch_id}/preview")
def preview_page(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    batch = _batch(request, batch_id)
    if batch.committed:
        return redirect(f"/admin/import/{batch_id}/summary", msg="This file has already been imported.")
    if not batch.mapping:
        return redirect(f"/admin/import/{batch_id}/columns", error="Please match the columns first.")
    _, file_rows, mapping, preview = _preview_for(request, batch)
    return render(
        request, "admin_import_preview.html", principal=principal, batch=batch, preview=preview,
        mapping=mapping, row_count=len(file_rows),
        errors=preview.errors[:MAX_LISTED], more_errors=max(0, len(preview.errors) - MAX_LISTED),
        notes=preview.warnings[:MAX_LISTED], more_notes=max(0, len(preview.warnings) - MAX_LISTED),
    )


# ══════════════════════════════════════════════════════════════════ step 4: the commit
@router.post("/import/{batch_id}/commit")
def commit_batch(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    # Held for the whole commit, photos included, so a data reset can never start underneath it (and an
    # import never starts while a reset is deleting photos). See backend/admin/reset.py.
    try:
        with reset_svc.import_lock(request.app.state.engine):
            return _commit_batch(request, batch_id, principal)
    except reset_svc.Busy as exc:
        return redirect(f"/admin/import/{batch_id}/preview", error=exc.message)


def _commit_batch(request: Request, batch_id: str, principal: Principal):
    batch = _batch(request, batch_id)
    if batch.committed:
        return redirect(f"/admin/import/{batch_id}/summary",
                        msg="This file was already imported. Nothing was written a second time.")
    if not batch.mapping:
        return redirect(f"/admin/import/{batch_id}/columns", error="Please match the columns first.")

    engine = request.app.state.engine
    _, _, _, preview = _preview_for(request, batch)
    if not preview.is_valid:
        return redirect(f"/admin/import/{batch_id}/preview",
                        error=f"{len(preview.errors)} row(s) still have to be fixed, so nothing was written.")

    zip_meta = batch.photos_zip
    if zip_meta is None:
        return _commit_students(request, batch, preview, principal)

    # The photo ZIP is used once, by this commit, and deleted afterwards whatever happens.
    try:
        store = _photo_store(request)
        if store.write_refusal:
            return redirect("/admin/import", error=store.write_refusal)
        try:
            inspect_photo_zip(batch.photos_zip_path)
        except ValueError:
            return redirect("/admin/import", error="The photo ZIP could no longer be read, so nothing was imported. "
                                                   "Please upload both files again.")
        with engine.connect() as conn:
            summary = commit_import(preview, conn, operator_id=principal.user_id, filename=batch.filename)
        try:
            report = import_photos_from_zip(
                batch.photos_zip_path, engine,
                excel_source=batch.upload_path if excel_engine_for(batch.filename) else None,
                excel_filename=batch.filename, store=store,
                max_workers=getattr(request.app.state.settings, "photo_import_concurrency", 4))
            photos = _zip_report(report, zip_meta, store)
        except Exception:
            logger.exception("photo import failed after the students were committed (batch %s)", batch_id)
            photos = {"source": "zip", "filename": zip_meta.get("filename"), "failed": True}
        return _save_summary(batch, summary, preview, photos)
    finally:
        import_staging.drop_photos_zip(batch)


def _commit_students(request: Request, batch, preview, principal: Principal):
    """The commit without a photo ZIP: students, then (optionally) the older photo-folder linker."""
    engine = request.app.state.engine
    with engine.connect() as conn:
        summary = commit_import(preview, conn, operator_id=principal.user_id, filename=batch.filename)

    photos = None
    if batch.photo_dir and pathlib.Path(batch.photo_dir).is_dir():
        with engine.connect() as conn:
            report = link_photos_by_prn(pathlib.Path(batch.photo_dir), conn)
        photos = {
            "folder": batch.photo_dir,
            "matched": report.matched_count,
            "unmatched_photos": [pathlib.Path(p).name for p in report.unmatched_photos],
            "unmatched_students": [s["prn"] for s in report.unmatched_students],
            "frozen_skipped": [s["prn"] for s in report.frozen_skipped],
        }

    return _save_summary(batch, summary, preview, photos)


def _save_summary(batch, summary, preview, photos):
    import_staging.update(batch, summary={
        "read": summary.read, "created": summary.created, "updated": summary.updated,
        "skipped": summary.skipped, "errors": summary.errors, "warnings": len(preview.warnings),
    }, photos=photos)
    if photos and photos.get("failed"):
        msg = f"{summary.created} student(s) added, but the photos could not be processed."
    elif photos and photos.get("source") == "zip":
        msg = f"{summary.created} student(s) added and {photos['matched']} photo(s) linked."
    else:
        msg = f"{summary.created} student(s) added."
    return redirect(f"/admin/import/{batch.batch_id}/summary", msg=msg)


# ══════════════════════════════════════════════════════════════════ step 5: the summary
@router.get("/import/{batch_id}/summary")
def summary_page(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    batch = _batch(request, batch_id)
    if not batch.committed:
        return redirect(f"/admin/import/{batch_id}/preview", error="This file has not been imported yet.")
    return render(request, "admin_import_summary.html", principal=principal, batch=batch,
                  summary=batch.summary, photos=batch.meta.get("photos"), max_listed=MAX_LISTED)

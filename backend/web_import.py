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
"""
from __future__ import annotations

import logging
import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from backend import import_staging
from backend.importer import FIELD_LABELS, REQUIRED_FIELDS, commit_import, detect_column_mapping, read_and_validate
from backend.photos import link_photos_by_prn
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
                 principal: Principal = Depends(require_admin)):
    content = await file.read()
    if not content:
        return redirect("/admin/import", error="That file is empty. Please choose the student list and try again.")

    folder = (photo_dir or "").strip()
    if folder and not pathlib.Path(folder).is_dir():
        return redirect("/admin/import", error=f"There is no folder at {folder} on this server.")

    batch = import_staging.create(request.app.state.settings, content=content, filename=file.filename or "upload.csv",
                                  uploaded_by=principal.user_id, photo_dir=folder)
    try:
        columns, _, _, _ = _preview_for(request, batch)
    except ValueError as exc:
        logger.info("import upload could not be read: %s", exc)
        return redirect("/admin/import",
                        error="That file could not be read. Please save it as CSV or XLSX and try again.")
    if not columns:
        return redirect("/admin/import", error="That file has no columns. Please check it and try again.")
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
    batch = _batch(request, batch_id)
    if batch.committed:
        return redirect(f"/admin/import/{batch_id}/summary",
                        msg="This file was already imported. Nothing was written a second time.")
    if not batch.mapping:
        return redirect(f"/admin/import/{batch_id}/columns", error="Please match the columns first.")

    engine = request.app.state.engine
    settings = request.app.state.settings
    _, _, _, preview = _preview_for(request, batch)
    if not preview.is_valid:
        return redirect(f"/admin/import/{batch_id}/preview",
                        error=f"{len(preview.errors)} row(s) still have to be fixed, so nothing was written.")

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

    import_staging.update(batch, summary={
        "read": summary.read, "created": summary.created, "updated": summary.updated,
        "skipped": summary.skipped, "errors": summary.errors, "warnings": len(preview.warnings),
    }, photos=photos)
    return redirect(f"/admin/import/{batch_id}/summary", msg=f"{summary.created} student(s) added.")


# ══════════════════════════════════════════════════════════════════ step 5: the summary
@router.get("/import/{batch_id}/summary")
def summary_page(request: Request, batch_id: str, principal: Principal = Depends(require_admin)):
    batch = _batch(request, batch_id)
    if not batch.committed:
        return redirect(f"/admin/import/{batch_id}/preview", error="This file has not been imported yet.")
    return render(request, "admin_import_summary.html", principal=principal, batch=batch,
                  summary=batch.summary, photos=batch.meta.get("photos"))

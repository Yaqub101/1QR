"""Admin console routes: dashboard, student search and journey, corrections, exceptions, audit, reports, exports.

EVERY route depends on `require_admin` (ADMIN or DEPUTY_ADMIN), so an operator gets 403 and a signed-out visitor
401, on the server, whatever the page shows. The JSON API lives under /admin/api; the pages under /admin. The
pages' forms call the same service functions as the API, so there is one implementation of each action.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text

from backend.admin import audit_view, corrections, dashboard, exporting
from backend.admin import exceptions as exceptions_svc
from backend.admin import reports as reports_svc
from backend.admin import students as students_svc
from backend.admin.corrections import CorrectionError
from backend import master_patch as master_patch_svc
from backend import passes as passes_svc
from backend import pass_cache
from backend import qr_tokens
from backend.audit import write_audit
from backend.security.deps import http_error, require_admin
from backend.security.sessions import Principal
from backend.web import redirect, render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _fail(exc: CorrectionError):
    return http_error(exc.status_code, exc.code, exc.message)


def _iso_or_400(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise http_error(400, "BAD_DATE", "Please give dates like 2026-10-15 or 2026-10-15T11:00.")
    return value


def _audit_filters(request: Request) -> dict:
    q = request.query_params
    return {"student": q.get("student") or None, "action": q.get("action") or None, "activity": q.get("activity") or None,
            "operator": q.get("operator") or None, "since": _iso_or_400(q.get("since")), "until": _iso_or_400(q.get("until"))}


# ══════════════════════════════════════════════════════════════════ JSON API
class ReverseBody(BaseModel):
    event_id: str
    reason: str = ""


class WaiveBody(BaseModel):
    student_id: str
    reason: str = ""


class ResolveBody(BaseModel):
    note: str = ""


class ReissueBody(BaseModel):
    reason: str = ""


class MasterPatchBody(BaseModel):
    changes: dict = {}
    reason: str = ""


# Which field of the student card each patchable field is currently shown from, so the form can put
# today's value in the box as a placeholder.
PATCH_SOURCE = {f: ("master_status" if f == "status" else f) for f in master_patch_svc.PATCHABLE_FIELDS}


@router.get("/api/dashboard")
def api_dashboard(request: Request):
    with request.app.state.engine.connect() as conn:
        return dashboard.snapshot(conn, request.app.state.settings)


@router.get("/api/students")
def api_students(request: Request, q: str = ""):
    with request.app.state.engine.connect() as conn:
        return {"students": students_svc.search(conn, q)["students"]}


@router.get("/api/students/{student_id}")
def api_student(request: Request, student_id: str):
    with request.app.state.engine.connect() as conn:
        data = students_svc.journey(conn, student_id, request.app.state.settings)
    if data is None:
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    return data


@router.post("/api/corrections/reverse")
def api_reverse(request: Request, body: ReverseBody, principal: Principal = Depends(require_admin)):
    try:
        return corrections.reverse_event(request.app.state.engine, principal=principal,
                                         event_id=body.event_id, reason=body.reason)
    except CorrectionError as exc:
        raise _fail(exc)


@router.post("/api/corrections/waive-return")
def api_waive(request: Request, body: WaiveBody, principal: Principal = Depends(require_admin)):
    try:
        return corrections.waive_return(request.app.state.engine, principal=principal,
                                        student_id=body.student_id, reason=body.reason)
    except CorrectionError as exc:
        raise _fail(exc)


@router.post("/api/corrections/waive-money")
def api_waive_money(request: Request, body: WaiveBody, principal: Principal = Depends(require_admin)):
    try:
        return corrections.waive_money(request.app.state.engine, principal=principal,
                                       student_id=body.student_id, reason=body.reason)
    except CorrectionError as exc:
        raise _fail(exc)


# ---- QR tokens and passes (Phase 4). A pass carries a LIVE token, so every route here is Admin-only and every
# download is logged. The audit log records ids and counts, never a token.
def _token_fail(exc: qr_tokens.TokenError):
    return http_error(exc.status_code, exc.code, exc.message)


def _page_param(name: str, raw: Optional[str], *, minimum: int) -> Optional[int]:
    """Blank (an empty form field) means "not given"; anything else must be a whole number >= minimum."""
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if not minimum <= value <= 1_000_000:
        raise http_error(400, "BAD_REQUEST", f"Please give a whole number from {minimum} to 1,000,000 for {name}.")
    return value


def _pdf(content: bytes, filename: str, warnings: int) -> Response:
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Pass-Warnings": str(warnings)})


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:60] or "x"


@router.post("/api/qr/generate-missing")
def api_generate_missing(request: Request, principal: Principal = Depends(require_admin)):
    """Give every ACTIVE student who has no QR one. Safe to press twice: the second time creates nothing."""
    result = qr_tokens.generate_missing_tokens(request.app.state.engine, operator_id=principal.user_id)
    return {"created": result.created, "active_students": result.active_students, "with_token": result.with_token}


@router.post("/api/students/{student_id}/reissue-qr")
def api_reissue_qr(request: Request, student_id: str, body: ReissueBody, principal: Principal = Depends(require_admin)):
    try:
        result = qr_tokens.reissue_token(request.app.state.engine, student_id=student_id, reason=body.reason,
                                         operator_id=principal.user_id)
    except qr_tokens.TokenError as exc:
        raise _token_fail(exc)
    return {"ok": True, "old_token_id": result.old_token_id, "new_token_id": result.new_token_id,
            "message": "A new QR has been issued. The old one no longer works. Print the new pass."}


def _one_pass(request: Request, student_id: str, principal: Principal) -> Response:
    engine = request.app.state.engine
    settings = request.app.state.settings
    if not qr_tokens.is_student_id(student_id):
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    with engine.connect() as conn:
        found = conn.execute(text("SELECT status FROM students WHERE id = :i"), {"i": student_id}).scalar_one_or_none()
        if found is None:
            raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
        if found != "ACTIVE":
            raise http_error(409, "STUDENT_NOT_ACTIVE", "That student is not active, so no pass can be printed.")
        rows_ = passes_svc.load_passes(conn, student_id=student_id)
        title = passes_svc.event_title(conn, settings.event_name)
    if not rows_ or not rows_[0]["token"]:
        raise http_error(409, "NO_TOKEN", "That student has no QR yet. Please generate the missing QR codes first.")
    data = passes_svc.to_pass_data(rows_)[0]
    result = passes_svc.render_single(data, title)
    with engine.begin() as conn:    # logged before the file is handed over: a download that cannot be logged is not served
        write_audit(conn, "PASS_DOWNLOADED", operator_id=principal.user_id, student_id=student_id,
                    details={"prn": data.prn, "warnings": [w.code for w in result.warnings]})
    return _pdf(result.pdf, f"pass-{_safe(data.prn)}.pdf", len(result.warnings))


@router.get("/api/students/{student_id}/pass.pdf")
def api_student_pass(request: Request, student_id: str, principal: Principal = Depends(require_admin)):
    return _one_pass(request, student_id, principal)


@router.get("/api/passes.pdf")
def api_passes(request: Request, school: Optional[str] = None, offset: Optional[str] = None, limit: Optional[str] = None,
               principal: Principal = Depends(require_admin)):
    """Sheets of four passes, ACTIVE students in sequence order. `school` narrows it; `offset` / `limit` split a big
    batch into several files (the whole 3,000-student list is a large PDF). Refuses, rather than printing a short
    sheet, if any selected student has no QR yet."""
    first = _page_param("offset", offset, minimum=0) or 0
    count = _page_param("limit", limit, minimum=1)
    school = (school or "").strip() or None
    engine = request.app.state.engine
    with engine.connect() as conn:
        selected = passes_svc.load_passes(conn, school=school, offset=first, limit=count)
        title = passes_svc.event_title(conn, request.app.state.settings.event_name)
    if not selected:
        raise http_error(404, "NO_STUDENTS", "There are no active students for that selection.")
    try:
        data = passes_svc.to_pass_data(selected)
    except qr_tokens.TokenError as exc:
        raise _token_fail(exc)
    result = pass_cache.get_or_render_sheets(
        title, selected, data, extra={"school": school, "offset": first, "limit": count}
    )
    with engine.begin() as conn:
        write_audit(conn, "PASSES_DOWNLOADED", operator_id=principal.user_id,
                    details={"count": result.count, "school": school, "offset": first, "limit": count,
                             "warnings": [{"prn": w.prn, "code": w.code} for w in result.warnings[:200]],
                             "warning_count": len(result.warnings)})
    name = f"passes-{_safe(school)}" if school else "passes-all"
    return _pdf(result.pdf, f"{name}-from-{first + 1}.pdf" if first else f"{name}.pdf", len(result.warnings))


@router.get("/api/exceptions")
def api_exceptions(request: Request, status: Optional[str] = None, type: Optional[str] = None):
    with request.app.state.engine.connect() as conn:
        return exceptions_svc.list_exceptions(conn, request.app.state.settings, status=status, type_=type)


@router.post("/api/exceptions/{exception_id}/resolve")
def api_resolve(request: Request, exception_id: str, body: ResolveBody, principal: Principal = Depends(require_admin)):
    try:
        return exceptions_svc.resolve(request.app.state.engine, principal=principal, exception_id=exception_id, note=body.note)
    except CorrectionError as exc:
        raise _fail(exc)


@router.get("/api/audit")
def api_audit(request: Request, limit: int = 100, offset: int = 0):
    with request.app.state.engine.connect() as conn:
        return audit_view.list_audit(conn, request.app.state.settings, limit=limit, offset=offset, **_audit_filters(request))


@router.get("/api/audit/export")
def api_audit_export(request: Request, format: str = "csv", principal: Principal = Depends(require_admin)):
    return _export(request, principal, "audit", format, _audit_filters(request))


@router.get("/api/reports")
def api_reports():
    return {"reports": reports_svc.catalogue()}


def _run_report(request: Request, key: str, params: dict, principal: Optional[Principal] = None, fmt: Optional[str] = None):
    """Run a report; when exporting, log the export in the SAME transaction as the read."""
    settings = request.app.state.settings
    try:
        with request.app.state.engine.begin() as conn:
            report = reports_svc.run(conn, settings, key, params)
            if fmt is not None:
                exporting.log_export(conn, principal, report, fmt, params)
        return report
    except reports_svc.UnknownReport:
        raise http_error(404, "REPORT_NOT_FOUND", "That report does not exist.")
    except reports_svc.BadReportRequest as exc:
        raise http_error(400, "BAD_REQUEST", str(exc))


def _export(request: Request, principal: Principal, key: str, fmt: str, params: dict) -> Response:
    fmt = (fmt or "csv").lower()
    if fmt not in exporting.FORMATS:
        raise http_error(400, "BAD_FORMAT", "Choose csv or xlsx.")
    report = _run_report(request, key, params, principal, fmt)
    render_fn, media_type = exporting.FORMATS[fmt]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(render_fn(report), media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{report.key}-{stamp}.{fmt}"', "Cache-Control": "no-store"})


@router.get("/api/reports/{key}")
def api_report(request: Request, key: str):
    report = _run_report(request, key, dict(request.query_params))
    return {"key": report.key, "title": report.title, "columns": [{"key": k, "label": label} for k, label in report.columns],
            "rows": report.rows, "totals": report.totals, "note": report.note}


@router.get("/api/reports/{key}/export")
def api_report_export(request: Request, key: str, format: str = "csv", principal: Principal = Depends(require_admin)):
    params = {k: v for k, v in request.query_params.items() if k != "format"}
    return _export(request, principal, key, format, params)


# ══════════════════════════════════════════════════════════════════ pages
@router.get("/dashboard")
def dashboard_page(request: Request, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = dashboard.snapshot(conn, request.app.state.settings)
    return render(request, "admin_dashboard.html", principal=principal, d=data)


@router.get("/dashboard/live")
def dashboard_live(request: Request, principal: Principal = Depends(require_admin)):
    """The dashboard body only: the page fetches this every few seconds and swaps it in."""
    with request.app.state.engine.connect() as conn:
        data = dashboard.snapshot(conn, request.app.state.settings)
    return render(request, "_dashboard_body.html", principal=principal, d=data)


@router.get("/students")
def students_page(request: Request, q: str = "", page: int = 1, principal: Principal = Depends(require_admin)):
    size = 25
    with request.app.state.engine.connect() as conn:
        result = students_svc.search(conn, q, limit=size, offset=(max(page, 1) - 1) * size)
    return render(request, "admin_students.html", principal=principal, q=q, 
                  found=result["students"], page=max(page, 1), pages=max(1, -(-result["total"] // size)),
                  query=urlencode({"q": q}) if q else "")


@router.get("/passes")
def passes_page(request: Request, page: int = 1, principal: Principal = Depends(require_admin)):
    size = 25
    with request.app.state.engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM students WHERE status = 'ACTIVE'")).scalar()
        rows = passes_svc.load_passes(conn, offset=(max(page, 1) - 1) * size, limit=size)
    return render(request, "admin_passes.html", principal=principal,
                  students=rows, page=max(page, 1), pages=max(1, -(-total // size)))


@router.post("/passes/generate")
def passes_generate(request: Request, principal: Principal = Depends(require_admin)):
    result = qr_tokens.generate_missing_tokens(request.app.state.engine, operator_id=principal.user_id)
    return redirect("/admin/passes", msg=f"Generated {result.created} new tokens.")


@router.get("/passes/download_all")
def passes_download_all(request: Request, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        rows = passes_svc.load_passes(conn)
        try:
            data = passes_svc.to_pass_data(rows)
        except qr_tokens.TokenError as e:
            return redirect("/admin/passes", error=e.message)
        event_name = passes_svc.event_title(conn, fallback="Convocation")
    result = pass_cache.get_or_render_sheets(event_name, rows, data)
    response = Response(content=result.pdf, media_type="application/pdf")
    response.headers["Content-Disposition"] = 'attachment; filename="all_passes.pdf"'
    return response


@router.get("/passes/download/{student_id}")
def passes_download_single(request: Request, student_id: str, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        rows = passes_svc.load_passes(conn, student_id=student_id)
        if not rows:
            return redirect("/admin/passes", error="Student not found or not active.")
        try:
            data = passes_svc.to_pass_data(rows)
        except qr_tokens.TokenError as e:
            return redirect("/admin/passes", error=e.message)
        event_name = passes_svc.event_title(conn, fallback="Convocation")
    result = passes_svc.render_single(data[0], event_name)
    response = Response(content=result.pdf, media_type="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="pass_{rows[0]["prn"]}.pdf"'
    return response



@router.get("/students/{student_id}")
def student_page(request: Request, student_id: str, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = students_svc.journey(conn, student_id, request.app.state.settings)
        qr = qr_tokens.status_for(conn, student_id) if data is not None else None
    if data is None:
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    return render(request, "admin_student.html", principal=principal, j=data, qr=qr,
                  patch_fields=master_patch_svc.PATCHABLE_FIELDS, patch_source=PATCH_SOURCE)


@router.post("/api/students/{student_id}/master-patch")
def api_master_patch(request: Request, student_id: str, body: MasterPatchBody,
                     principal: Principal = Depends(require_admin)):
    """Change a frozen student's master fields. The ONLY way frozen master data ever moves."""
    try:
        result = master_patch_svc.apply_master_patch(
            request.app.state.engine, student_id=student_id, changes=body.changes, reason=body.reason,
            operator_id=principal.user_id)
    except master_patch_svc.MasterPatchError as exc:
        raise http_error(exc.status_code, exc.code, exc.message)
    return {"ok": True, "student_id": result.student_id, "prn": result.prn, "changed": result.changed,
            "snapshot_refreshed": result.snapshot_refreshed,
            "message": f"{len(result.changed)} field(s) changed. The change and your reason are in the audit log."}


@router.post("/students/{student_id}/master-patch")
async def master_patch_form(request: Request, student_id: str, principal: Principal = Depends(require_admin)):
    """The form behind the same action. An empty box means "leave this one alone", so an Admin who
    wants to change one field does not have to retype the other seven."""
    form = await request.form()
    changes = {f: form[f].strip() for f in master_patch_svc.PATCHABLE_FIELDS
               if f in form and str(form[f]).strip() != ""}
    try:
        result = master_patch_svc.apply_master_patch(
            request.app.state.engine, student_id=student_id, changes=changes, reason=form.get("reason", ""),
            operator_id=principal.user_id)
    except master_patch_svc.MasterPatchError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    changed = ", ".join(master_patch_svc.PATCHABLE_FIELDS[f] for f in result.changed)
    return redirect(f"/admin/students/{student_id}", msg=f"Master patch applied: {changed}. It is in the audit log.")


@router.post("/students/{student_id}/reissue-qr")
def reissue_form(request: Request, student_id: str, reason: str = Form(""), principal: Principal = Depends(require_admin)):
    try:
        qr_tokens.reissue_token(request.app.state.engine, student_id=student_id, reason=reason,
                                operator_id=principal.user_id)
    except qr_tokens.TokenError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg="A new QR has been issued. The old one no longer works. Print the new pass.")


@router.post("/students/{student_id}/reverse")
def reverse_form(request: Request, student_id: str, event_id: str = Form(...), reason: str = Form(""),
                 principal: Principal = Depends(require_admin)):
    try:
        result = corrections.reverse_event(request.app.state.engine, principal=principal,
                                           event_id=event_id, reason=reason)
    except CorrectionError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg=result["message"])


@router.post("/students/{student_id}/waive-return")
def waive_form(request: Request, student_id: str, reason: str = Form(""), principal: Principal = Depends(require_admin)):
    try:
        result = corrections.waive_return(request.app.state.engine, principal=principal,
                                          student_id=student_id, reason=reason)
    except CorrectionError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg=result["message"])


@router.post("/students/{student_id}/waive-money")
def waive_money_form(request: Request, student_id: str, reason: str = Form(""),
                     principal: Principal = Depends(require_admin)):
    try:
        result = corrections.waive_money(request.app.state.engine, principal=principal,
                                         student_id=student_id, reason=reason)
    except CorrectionError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg=result["message"])


@router.get("/exceptions")
def exceptions_page(request: Request, status: str = "OPEN", principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = exceptions_svc.list_exceptions(conn, request.app.state.settings, status=None if status == "ALL" else status)
        reissued_count = int(conn.execute(text("SELECT count(*) FROM audit_log WHERE action = 'QR_REISSUED'")).scalar_one())
    return render(request, "admin_exceptions.html", principal=principal, data=data, status=status, reissued_count=reissued_count)


@router.post("/exceptions/{exception_id}/resolve")
def resolve_form(request: Request, exception_id: str, note: str = Form(""), principal: Principal = Depends(require_admin)):
    try:
        result = exceptions_svc.resolve(request.app.state.engine, principal=principal, exception_id=exception_id, note=note)
    except CorrectionError as exc:
        return redirect("/admin/exceptions", error=exc.message)
    return redirect("/admin/exceptions", msg=result["message"])


@router.get("/audit")
def audit_page(request: Request, page: int = 1, principal: Principal = Depends(require_admin)):
    filters = _audit_filters(request)
    size = 100
    with request.app.state.engine.connect() as conn:
        data = audit_view.list_audit(conn, request.app.state.settings, limit=size, offset=(max(page, 1) - 1) * size, **filters)
        actions = audit_view.actions(conn)
    query = urlencode({k: v for k, v in filters.items() if v})
    return render(request, "admin_audit.html", principal=principal, data=data, actions=actions, filters=filters, page=max(page, 1),
                  pages=max(1, -(-data["total"] // size)), query=query)


@router.get("/reports")
def reports_page(request: Request, principal: Principal = Depends(require_admin)):
    groups: dict[str, list] = {}
    for item in reports_svc.catalogue():
        groups.setdefault(item["group"], []).append(item)
    return render(request, "admin_reports.html", principal=principal, groups=groups, report=None, active_nav="reports")


@router.get("/reports/{key}")
def report_page(request: Request, key: str, principal: Principal = Depends(require_admin)):
    params = dict(request.query_params)
    report = _run_report(request, key, params)
    groups: dict[str, list] = {}
    for item in reports_svc.catalogue():
        groups.setdefault(item["group"], []).append(item)
    return render(request, "admin_reports.html", principal=principal, groups=groups, report=report,
                  query=urlencode(params), preview_rows=report.rows[:500], active_nav="reports", current_key=key)

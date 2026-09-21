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

from backend.admin import audit_view, corrections, dashboard, exporting
from backend.admin import exceptions as exceptions_svc
from backend.admin import reports as reports_svc
from backend.admin import students as students_svc
from backend.admin.corrections import CorrectionError
from backend.security import ownership
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


@router.get("/api/dashboard")
def api_dashboard(request: Request):
    with request.app.state.engine.connect() as conn:
        return dashboard.snapshot(conn, request.app.state.settings)


@router.get("/api/students")
def api_students(request: Request, q: str = ""):
    with request.app.state.engine.connect() as conn:
        return {"students": students_svc.search(conn, q)}


@router.get("/api/students/{student_id}")
def api_student(request: Request, student_id: str):
    with request.app.state.engine.connect() as conn:
        data = students_svc.journey(conn, student_id, request.app.state.settings, request.app.state.venue_guard)
    if data is None:
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    return data


@router.post("/api/corrections/reverse")
def api_reverse(request: Request, body: ReverseBody, principal: Principal = Depends(require_admin)):
    try:
        return corrections.reverse_event(request.app.state.engine, guard=request.app.state.venue_guard, principal=principal,
                                         event_id=body.event_id, reason=body.reason)
    except CorrectionError as exc:
        raise _fail(exc)


@router.post("/api/corrections/waive-return")
def api_waive(request: Request, body: WaiveBody, principal: Principal = Depends(require_admin)):
    try:
        return corrections.waive_return(request.app.state.engine, guard=request.app.state.venue_guard, principal=principal,
                                        student_id=body.student_id, reason=body.reason)
    except CorrectionError as exc:
        raise _fail(exc)


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
def students_page(request: Request, q: str = "", principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        found = students_svc.search(conn, q)
    return render(request, "admin_students.html", principal=principal, q=q, found=found)


@router.get("/students/{student_id}")
def student_page(request: Request, student_id: str, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = students_svc.journey(conn, student_id, request.app.state.settings, request.app.state.venue_guard)
    if data is None:
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    return render(request, "admin_student.html", principal=principal, j=data,
                  here=request.app.state.settings.venue_id, venue_of=ownership.ACTIVITY_OWNER)


@router.post("/students/{student_id}/reverse")
def reverse_form(request: Request, student_id: str, event_id: str = Form(...), reason: str = Form(""),
                 principal: Principal = Depends(require_admin)):
    try:
        result = corrections.reverse_event(request.app.state.engine, guard=request.app.state.venue_guard, principal=principal,
                                           event_id=event_id, reason=reason)
    except CorrectionError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg=result["message"])


@router.post("/students/{student_id}/waive-return")
def waive_form(request: Request, student_id: str, reason: str = Form(""), principal: Principal = Depends(require_admin)):
    try:
        result = corrections.waive_return(request.app.state.engine, guard=request.app.state.venue_guard, principal=principal,
                                          student_id=student_id, reason=reason)
    except CorrectionError as exc:
        return redirect(f"/admin/students/{student_id}", error=exc.message)
    return redirect(f"/admin/students/{student_id}", msg=result["message"])


@router.get("/exceptions")
def exceptions_page(request: Request, status: str = "OPEN", principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = exceptions_svc.list_exceptions(conn, request.app.state.settings, status=None if status == "ALL" else status)
    return render(request, "admin_exceptions.html", principal=principal, data=data, status=status)


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
    return render(request, "admin_reports.html", principal=principal, groups=groups, report=None)


@router.get("/reports/{key}")
def report_page(request: Request, key: str, principal: Principal = Depends(require_admin)):
    params = dict(request.query_params)
    report = _run_report(request, key, params)
    return render(request, "admin_reports.html", principal=principal, groups={}, report=report,
                  query=urlencode(params), preview_rows=report.rows[:500])

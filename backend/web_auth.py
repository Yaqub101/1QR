"""Sign-in, sign-out, "who am I", and the station screen shell."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from backend import passes as passes_svc, qr_tokens
from backend.audit import write_audit
from backend.engine import registry
from backend.engine.activities import ACTIVITY_CONFIGS
from backend.security import permissions, sessions
from backend.security.deps import (
    SESSION_COOKIE,
    ActivityAccess,
    http_error,
    optional_user,
    require_activity_access,
    require_user,
    token_from_request,
)
from backend.security.login import LoginResult, attempt_login
from backend.security.sessions import Principal
from backend.web import landing_url, redirect, render, set_session_cookie

router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


class StationReissueBody(BaseModel):
    student_id: str
    reason: str


class StationWaiveBody(BaseModel):
    student_id: str
    reason: str
    username: str
    password: str


def _login(request: Request, username: str, password: str) -> LoginResult:
    settings = request.app.state.settings
    with request.app.state.engine.begin() as conn:  # commit even on a refused login: the audit row is kept
        return attempt_login(conn, settings=settings, username=username, password=password)


@router.get("/")
def home(principal: Optional[Principal] = Depends(optional_user)):
    if principal is None:
        return redirect("/login")
    return redirect(landing_url(principal.is_admin, principal.role))


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login_form(request: Request, username: str = Form(...), password: str = Form(...)):
    result = _login(request, username, password)
    if result.error:
        return render(request, "login.html", status_code=result.error.status_code, error=result.error.message)
    response = redirect(landing_url(permissions.is_admin_role(result.role), result.role))
    set_session_cookie(response, result.token, request.app.state.settings)
    return response


@router.post("/api/login")
def login_api(request: Request, body: LoginBody):
    result = _login(request, body.username, body.password)
    if result.error:
        raise http_error(result.error.status_code, result.error.code, result.error.message)
    response = JSONResponse(
        {
            "user": {"id": str(result.user_id), "username": result.username, "full_name": result.full_name,
                     "role": result.role},
            "token": result.token,
        }
    )
    set_session_cookie(response, result.token, request.app.state.settings)
    return response


def _end_session(request: Request) -> None:
    token = token_from_request(request)
    if token:
        with request.app.state.engine.begin() as conn:
            sessions.revoke_token(conn, token)


@router.post("/logout")
def logout_form(request: Request, _: Principal = Depends(require_user)):
    _end_session(request)
    response = redirect("/login", msg="You have been signed out.")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/api/logout")
def logout_api(request: Request, _: Principal = Depends(require_user)):
    _end_session(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/api/me")
def me(principal: Principal = Depends(require_user)):
    return {
        "user_id": str(principal.user_id),
        "username": principal.username,
        "full_name": principal.full_name,
        "role": principal.role,
        "is_admin": principal.is_admin,
        "permissions": sorted(principal.permissions),
    }


@router.get("/station/registry")
def registry_screen(request: Request, principal: Principal = Depends(require_user)):
    """The Registry desk: one scan point for entry (Reporting + Robe) and the robe return. The button's
    label comes from the server with each scan, because the desk works out which step the student is at."""
    if not registry.can_use(principal.role):
        raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
    return render(request, "station.html", principal=principal, activity=registry.STATION, title="Registry",
                  config=SimpleNamespace(confirm_label="CONFIRM"))


@router.get("/station/{activity}")
def station_screen(request: Request, access: ActivityAccess = Depends(require_activity_access("activity"))):
    """The operator screen for one activity. Its behaviour comes from the engine (backend/engine)."""
    principal = access.principal
    return render(request, "station.html", principal=principal, activity=access.activity,
                  config=ACTIVITY_CONFIGS[access.activity])


@router.get("/station/api/pass/{student_id}")
def station_download_pass(request: Request, student_id: str, principal: Principal = Depends(require_user)):
    """Allow Registry operators and Admins to download student passes directly at the station desk."""
    if not permissions.has_permission(principal.role, permissions.PASS_MANAGEMENT_PERMISSION):
        raise http_error(403, "FORBIDDEN", "Only Registry operators and Admins can download passes.")
    if not qr_tokens.is_student_id(student_id):
        raise http_error(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    engine = request.app.state.engine
    settings = request.app.state.settings
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
    with engine.begin() as conn:
        write_audit(conn, "PASS_DOWNLOADED", operator_id=principal.user_id, student_id=student_id,
                    details={"prn": data.prn, "station": "REGISTRY", "warnings": [w.code for w in result.warnings]})
    safe_prn = "".join(c for c in (data.prn or "") if c.isalnum() or c in "-_") or "student"
    return Response(
        result.pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="pass-{safe_prn}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/station/api/reissue-pass")
def station_reissue_pass(request: Request, body: StationReissueBody, principal: Principal = Depends(require_user)):
    """Allow Registry operators and Admins to reissue a QR pass with an audited reason."""
    if not permissions.has_permission(principal.role, permissions.PASS_MANAGEMENT_PERMISSION):
        raise http_error(403, "FORBIDDEN", "Only Registry operators and Admins can reissue passes.")
    try:
        reason = qr_tokens.clean_reason(body.reason)
        result = qr_tokens.reissue_token(
            request.app.state.engine,
            student_id=body.student_id,
            reason=reason,
            operator_id=principal.user_id,
        )
    except qr_tokens.TokenError as exc:
        raise http_error(exc.status_code, exc.code, exc.message)
    return {
        "ok": True,
        "student_id": body.student_id,
        "old_token_id": result.old_token_id,
        "new_token_id": result.new_token_id,
        "message": "New QR pass issued. Previous QR invalidated.",
        "pass_url": f"/station/api/pass/{body.student_id}",
    }


@router.post("/station/api/waive-robe")
def station_waive_robe(request: Request, body: StationWaiveBody, principal: Principal = Depends(require_user)):
    """Allow Registry operators and Admins to record a robe return waiver / lost robe exception."""
    if not (principal.is_admin or principal.role == "REGISTRY"):
        raise http_error(403, "FORBIDDEN", "Only Registry operators and Admins can record robe waivers.")
    from backend.admin.corrections import _waive, clean_reason, CorrectionError
    from sqlalchemy.exc import IntegrityError
    try:
        reason = clean_reason(body.reason)
    except CorrectionError as exc:
        raise http_error(exc.status_code, exc.code, exc.message)
    engine = request.app.state.engine
    try:
        with engine.begin() as conn:
            result = _waive(conn, "THOBE_RETURN", principal=principal, student_id=body.student_id, reason=reason)
        return result
    except CorrectionError as exc:
        raise http_error(exc.status_code, exc.code, exc.message)
    except IntegrityError as exc:
        raise http_error(409, "ALREADY_RETURNED", "That robe is already recorded as returned or waived.")
    except Exception as exc:
        raise http_error(503, "TEMPORARY", "One moment, please try again.")

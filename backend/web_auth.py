"""Sign-in, sign-out, "who am I", and the station screen shell."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.engine.activities import ACTIVITY_CONFIGS
from backend.security import permissions, sessions
from backend.security.deps import (
    DEVICE_COOKIE,
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
from backend.stations import list_stations
from backend.web import landing_url, redirect, render, set_session_cookie

router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str
    # Anything else in the body (e.g. an "activity") is ignored: the station decides the activity.


def _login(request: Request, username: str, password: str) -> LoginResult:
    settings = request.app.state.settings
    with request.app.state.engine.begin() as conn:  # commit even on a refused login: the audit row is kept
        return attempt_login(
            conn, settings=settings, username=username, password=password,
            device_token=request.cookies.get(DEVICE_COOKIE),
        )


@router.get("/")
def home(principal: Optional[Principal] = Depends(optional_user)):
    if principal is None:
        return redirect("/login")
    return redirect(landing_url(principal.is_admin, principal.station_activity))


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login")
def login_form(request: Request, username: str = Form(...), password: str = Form(...)):
    result = _login(request, username, password)
    if result.error:
        return render(request, "login.html", status_code=result.error.status_code, error=result.error.message)
    response = redirect(landing_url(permissions.is_admin_role(result.role), result.activity))
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
            "station_id": result.station_id,
            "activity": result.activity,
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
        "station_id": principal.station_id,
        "activity": principal.station_activity,  # from the station binding, not chosen by the operator
        "permissions": sorted(principal.permissions),
    }


@router.get("/station/{activity}")
def station_screen(request: Request, access: ActivityAccess = Depends(require_activity_access("activity"))):
    """The operator screen for one activity. Its behaviour comes from the engine (backend/engine)."""
    principal = access.principal
    stations = []
    if principal.is_admin:  # an Admin picks which station of this venue to act as
        with request.app.state.engine.connect() as conn:
            stations = [
                s for s in list_stations(conn, request.app.state.settings.venue_id)
                if s["active"] and s["activity"] == access.activity
            ]
    return render(
        request, "station.html", principal=principal, activity=access.activity,
        config=ACTIVITY_CONFIGS[access.activity], station_id=None if principal.is_admin else principal.station_id,
        stations=stations,
    )

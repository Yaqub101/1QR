"""Shared bits for the server-rendered screens (Jinja2, plain forms, no build step)."""
from __future__ import annotations

import pathlib
from typing import Optional
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.security import ownership
from backend.security.deps import SESSION_COOKIE

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["activity_label"] = ownership.ACTIVITY_LABEL
templates.env.globals["role_label"] = {
    "ADMIN": "Admin",
    "DEPUTY_ADMIN": "Deputy Admin",
    **{a: f"{label} operator" for a, label in ownership.ACTIVITY_LABEL.items()},
}


def slug(activity: str) -> str:
    return activity.lower().replace("_", "-")


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("error", request.query_params.get("error"))
    context.setdefault("settings", request.app.state.settings)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str, *, msg: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("error", error)) if v}
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def set_session_cookie(response, token: str, settings) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure,
        max_age=settings.session_max_hours * 3600, path="/",
    )


def landing_url(role_is_admin: bool, role: Optional[str]) -> str:
    """An Admin/Deputy lands on the admin console; an operator lands on their own activity's
    screen -- their role names the activity directly, one role per account."""
    if role_is_admin or not role:
        return "/admin"
    return f"/station/{slug(role)}"

"""Shared bits for the server-rendered screens (Jinja2, plain forms, no build step)."""
from __future__ import annotations

import hashlib
import pathlib
from typing import Optional
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from backend.security import ownership
from backend.security.deps import SESSION_COOKIE

TEMPLATES_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_asset_versions: dict = {}  # (path, size, mtime) -> version: a file is hashed again only after it changes


def versioned_asset(static_dir: pathlib.Path, name: str) -> str:
    """/static/<name>?v=<first 10 hex of the file's SHA-256>. A deploy that changes a script gives it a new
    address, so no browser keeps running an old copy; an unchanged file keeps its address and its cache.
    A missing file raises instead of rendering a link to nothing."""
    path = pathlib.Path(static_dir) / name
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    version = _asset_versions.get(key)
    if version is None:
        version = _asset_versions[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={version}"


templates.env.globals["asset_url"] = lambda name: versioned_asset(STATIC_DIR, name)
templates.env.globals["activity_label"] = ownership.ACTIVITY_LABEL
templates.env.globals["role_label"] = {
    "ADMIN": "Admin",
    "DEPUTY_ADMIN": "Deputy Admin",
    "REGISTRY": "Registry operator (Reporting, Robe, Robe Return)",
    "CALLER": "Caller (read-only Caller screen)",
    **{a: f"{label} operator" for a, label in ownership.ACTIVITY_LABEL.items()
       if a not in ("REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED", "THOBE_RETURN", "MONEY_RETURNED")},
}


def slug(activity: str) -> str:
    return activity.lower().replace("_", "-")


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("error", request.query_params.get("error"))
    context.setdefault("settings", request.app.state.settings)
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


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
    if role == "CALLER":
        return "/caller"
    return f"/station/{slug(role)}"

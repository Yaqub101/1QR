"""FastAPI dependencies: the reusable guards every endpoint goes through.

    require_user(request)                 any signed-in user          -> 401 otherwise
    require_admin(principal)              ADMIN or DEPUTY_ADMIN       -> 403 otherwise
    require_activity_access("activity")   the operator's ROLE decides which activity screen
                                           they may use, from any browser (docs/ARCHITECTURE_PIVOT.md)

Endpoints never compare role names themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request

from backend.security import ownership, permissions, sessions
from backend.security.sessions import Principal

SESSION_COOKIE = "session"


def http_error(status: int, code: str, message: str, headers: Optional[dict] = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def token_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def _unauthorized(code: str, message: str) -> HTTPException:
    return http_error(401, code, message, headers={"WWW-Authenticate": "Bearer"})


def optional_user(request: Request) -> Optional[Principal]:
    token = token_from_request(request)
    if not token:
        return None
    settings = request.app.state.settings
    with request.app.state.engine.begin() as conn:
        return sessions.load_principal(conn, token, idle_minutes=settings.session_idle_minutes)


def require_user(request: Request) -> Principal:
    token = token_from_request(request)
    if not token:
        raise _unauthorized("NOT_SIGNED_IN", "Please sign in.")
    settings = request.app.state.settings
    with request.app.state.engine.begin() as conn:
        principal = sessions.load_principal(conn, token, idle_minutes=settings.session_idle_minutes)
        ended = principal is None and sessions.session_existed(conn, token)
    if principal is None:
        if ended:
            raise _unauthorized("SESSION_EXPIRED", "Your session has ended. Please sign in again.")
        raise _unauthorized("NOT_SIGNED_IN", "Please sign in.")
    return principal


def require_admin(principal: Principal = Depends(require_user)) -> Principal:
    if not permissions.has_permission(principal.role, permissions.ADMIN_PERMISSION):
        raise http_error(403, "FORBIDDEN", "That page is for the Admin only.")
    return principal


@dataclass(frozen=True)
class ActivityAccess:
    principal: Principal
    activity: str


def require_activity_access(param: str = "activity") -> Callable[..., ActivityAccess]:
    """A station screen: the operator's role must allow this activity. Admin/Deputy may use any."""

    def dependency(request: Request, principal: Principal = Depends(require_user)) -> ActivityAccess:
        try:
            activity = ownership.normalize_activity(request.path_params.get(param, ""))
        except ownership.UnknownActivityError as exc:
            raise http_error(404, exc.code, exc.message) from exc
        if not permissions.can_use_activity(principal.role, activity):
            raise http_error(403, "FORBIDDEN", "That screen is not part of your role.")
        return ActivityAccess(principal=principal, activity=activity)

    return dependency

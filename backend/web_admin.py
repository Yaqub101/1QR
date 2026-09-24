"""Admin screens: users."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request

from backend import users as users_svc
from backend.admin import dashboard
from backend.security import permissions
from backend.security.deps import require_admin
from backend.security.passwords import MIN_ADMIN_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from backend.security.sessions import Principal
from backend.users import AccountError
from backend.web import redirect, render

router = APIRouter(prefix="/admin")


def _uuid_or_404(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AccountError("USER_NOT_FOUND", "That user does not exist.", 404)


@router.get("")
def admin_home(request: Request, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        data = dashboard.snapshot(conn, request.app.state.settings)
    return render(request, "admin_home.html", principal=principal, d=data, active_nav="dashboard")


# ── users ───────────────────────────────────────────────────────────────────
@router.get("/users")
def users_page(request: Request, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        users = users_svc.list_users(conn)
    return render(
        request, "admin_users.html", principal=principal, users=users,
        operator_roles=permissions.OPERATOR_ROLES, min_length=MIN_PASSWORD_LENGTH,
    )


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    principal: Principal = Depends(require_admin),
):
    try:
        if role not in permissions.OPERATOR_ROLES:  # Admin/Deputy accounts come from the seed script only
            raise AccountError("BAD_ROLE", "Only operator accounts can be created here.")
        with request.app.state.engine.begin() as conn:
            users_svc.create_user(conn, username=username, password=password, role=role, full_name=full_name,
                                  actor_id=principal.user_id)
    except AccountError as exc:
        return redirect("/admin/users", error=exc.message)
    return redirect("/admin/users", msg=f"Account {username.strip()} created.")


@router.post("/users/{user_id}/active")
def set_user_active(request: Request, user_id: str, active: str = Form(...),
                    principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            users_svc.set_user_active(conn, _uuid_or_404(user_id), active == "1", actor_id=principal.user_id)
    except AccountError as exc:
        return redirect("/admin/users", error=exc.message)
    return redirect("/admin/users", msg="Account switched on." if active == "1" else "Account switched off.")


@router.post("/users/{user_id}/password")
def reset_password(request: Request, user_id: str, password: str = Form(...),
                   principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            users_svc.reset_password(conn, _uuid_or_404(user_id), password, actor_id=principal.user_id)
    except AccountError as exc:
        return redirect("/admin/users", error=exc.message)
    return redirect("/admin/users", msg="Password changed. They have been signed out.")


@router.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: str, principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            users_svc.delete_user(conn, _uuid_or_404(user_id), actor_id=principal.user_id)
    except AccountError as exc:
        return redirect("/admin/users", error=exc.message)
    return redirect("/admin/users", msg="Account deleted.")

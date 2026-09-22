"""Admin screens: users, stations, and binding this laptop to a station."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request

from backend import stations as stations_svc
from backend import users as users_svc
from backend.security import ownership, permissions
from backend.security.deps import DEVICE_COOKIE, require_admin, require_venue_mode
from backend.security.passwords import MIN_ADMIN_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from backend.security.sessions import Principal
from backend.users import AccountError
from backend.web import redirect, render, set_device_cookie

router = APIRouter(prefix="/admin")


def _uuid_or_404(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AccountError("USER_NOT_FOUND", "That user does not exist.", 404)


def _venue(request: Request) -> str:
    return request.app.state.settings.venue_id


@router.get("")
def admin_home(request: Request, principal: Principal = Depends(require_admin)):
    return render(request, "admin_home.html", principal=principal)


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


# ── stations ────────────────────────────────────────────────────────────────
@router.get("/stations", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def stations_page(request: Request, principal: Principal = Depends(require_admin)):
    venue = _venue(request)
    with request.app.state.engine.connect() as conn:
        stations = stations_svc.list_stations(conn, venue)
    owned = [a for a in ownership.ACTIVITIES if ownership.ACTIVITY_OWNER[a] == venue]
    return render(request, "admin_stations.html", principal=principal, stations=stations, owned_activities=owned,
                  venue=venue)


@router.post("/stations", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def create_station(request: Request, station_id: str = Form(...), activity: str = Form(...),
                   principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            stations_svc.create_station(conn, venue_id=_venue(request), station_id=station_id, activity=activity,
                                        actor_id=principal.user_id)
    except AccountError as exc:
        return redirect("/admin/stations", error=exc.message)
    return redirect("/admin/stations", msg=f"Station {station_id.strip()} created.")


@router.post("/stations/{station_id}/active", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def set_station_active(request: Request, station_id: str, active: str = Form(...),
                       principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            stations_svc.set_station_active(conn, station_id, active == "1", actor_id=principal.user_id,
                                            venue_id=_venue(request))
    except AccountError as exc:
        return redirect("/admin/stations", error=exc.message)
    return redirect("/admin/stations", msg="Station switched on." if active == "1" else "Station switched off.")


# ── binding: the one-tap flow for swapping in a spare laptop ─────────────────
@router.get("/bind", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def bind_page(request: Request, principal: Principal = Depends(require_admin)):
    venue = _venue(request)
    with request.app.state.engine.connect() as conn:
        stations = [s for s in stations_svc.list_stations(conn, venue) if s["active"]]
        here = stations_svc.station_for_device(conn, request.cookies.get(DEVICE_COOKIE), venue)
    return render(request, "admin_bind.html", principal=principal, stations=stations, this_laptop=here, venue=venue)


@router.post("/bind/{station_id}", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def bind(request: Request, station_id: str, principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            token = stations_svc.bind_station(
                conn, station_id, actor_id=principal.user_id,
                previous_device_token=request.cookies.get(DEVICE_COOKIE), venue_id=_venue(request),
            )
    except AccountError as exc:
        return redirect("/admin/bind", error=exc.message)
    response = redirect("/admin/bind", msg=f"This laptop is now station {station_id}. Sign out and hand it over.")
    set_device_cookie(response, token, request.app.state.settings)
    return response


@router.post("/unbind/{station_id}", dependencies=[Depends(require_admin), Depends(require_venue_mode)])
def unbind(request: Request, station_id: str, principal: Principal = Depends(require_admin)):
    try:
        with request.app.state.engine.begin() as conn:
            stations_svc.unbind_station(conn, station_id, actor_id=principal.user_id, venue_id=_venue(request))
    except AccountError as exc:
        return redirect("/admin/bind", error=exc.message)
    return redirect("/admin/bind", msg=f"Station {station_id} no longer has a laptop.")

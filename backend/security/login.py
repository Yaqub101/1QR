"""Login: credentials -> (role, station rules) -> session.

Expected failures are returned, not raised, so the caller can COMMIT the audit row
that records them before answering. Every message is one plain sentence
(golden rule 11).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend import stations as stations_svc
from backend.audit import write_audit
from backend.security import ownership, passwords, permissions
from backend.security.sessions import create_session


@dataclass(frozen=True)
class LoginError:
    status_code: int
    code: str
    message: str


@dataclass(frozen=True)
class LoginResult:
    error: Optional[LoginError] = None
    token: Optional[str] = None
    user_id: object = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    station_id: Optional[str] = None
    activity: Optional[str] = None  # from the station binding; None for Admin/Deputy


def _fail(status: int, code: str, message: str) -> LoginResult:
    return LoginResult(error=LoginError(status, code, message))


BAD_CREDENTIALS = ("BAD_CREDENTIALS", "Wrong username or password.")


def attempt_login(
    conn: Connection, *, settings, username: str, password: str, device_token: Optional[str]
) -> LoginResult:
    user = conn.execute(
        text("SELECT id, username, full_name, role, active, password_hash FROM users WHERE lower(username) = lower(:u)"),
        {"u": (username or "").strip()},
    ).mappings().one_or_none()

    if user is None:
        passwords.verify_against_dummy(password or "")
        write_audit(conn, "LOGIN_FAILED", venue_id=settings.venue_id, details={"reason": "unknown user"})
        return _fail(401, *BAD_CREDENTIALS)

    if not passwords.verify_password(password or "", user["password_hash"]):
        write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], venue_id=settings.venue_id,
                    details={"reason": "wrong password"})
        return _fail(401, *BAD_CREDENTIALS)

    # Only someone who proved the password is told the account is off.
    if not user["active"]:
        write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], venue_id=settings.venue_id,
                    details={"reason": "account disabled"})
        return _fail(403, "ACCOUNT_DISABLED", "This account is switched off. Please call the Admin.")

    role = user["role"]
    station_id = None
    activity = None
    if not permissions.is_admin_role(role):
        if settings.mode != "venue":
            return _fail(403, "OPERATORS_NOT_HERE", "Operators sign in at their own venue, not here.")
        station = stations_svc.station_for_device(conn, device_token, settings.venue_id)
        if station is None:
            write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], venue_id=settings.venue_id,
                        details={"reason": "laptop not assigned to a station"})
            return _fail(403, "NO_STATION", "This laptop is not assigned to a station. Please call the Admin.")
        if station["activity"] != role:  # the station decides; the operator's role must fit it
            label = ownership.ACTIVITY_LABEL[station["activity"]]
            write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], venue_id=settings.venue_id,
                        station_id=station["station_id"], details={"reason": "role does not fit station"})
            return _fail(403, "STATION_MISMATCH", f"This station is for {label}, which is not your role.")
        station_id, activity = station["station_id"], station["activity"]

    token = create_session(conn, user_id=user["id"], station_id=station_id, max_hours=settings.session_max_hours)
    write_audit(conn, "LOGIN", operator_id=user["id"], station_id=station_id, venue_id=settings.venue_id)
    return LoginResult(
        token=token, user_id=user["id"], username=user["username"], full_name=user["full_name"],
        role=role, station_id=station_id, activity=activity,
    )

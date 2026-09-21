"""Stations and the laptop <-> station binding (Phase 5).

A station belongs to one venue and one activity for life (database trigger). A
laptop becomes that station by holding its secret device token; the station,
not the operator, then decides which activity the laptop does (golden rule 2).
Only a hash of the token is stored. Rebinding is a single Admin action.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit
from backend.security import ownership
from backend.security.sessions import hash_token, revoke_station_sessions
from backend.users import AccountError

_STATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def create_station(conn: Connection, *, venue_id: str, station_id: str, activity: str, actor_id=None) -> None:
    station_id = (station_id or "").strip()
    if not _STATION_ID.match(station_id):
        raise AccountError("BAD_STATION_ID", "The station name can use letters, numbers and dashes, up to 32 characters.")
    try:
        activity = ownership.normalize_activity(activity)
    except ownership.UnknownActivityError as exc:
        raise AccountError("BAD_ACTIVITY", exc.message) from exc
    try:  # same single-writer rule as every write path
        ownership.VenueGuard(mode="venue", venue_id=venue_id).ensure_can_originate(activity)
    except ownership.OwnershipError as exc:
        raise AccountError("WRONG_VENUE", exc.message, 403) from exc
    try:
        with conn.begin_nested():
            conn.execute(
                text("INSERT INTO stations (station_id, venue_id, activity) VALUES (:s, :v, :a)"),
                {"s": station_id, "v": venue_id, "a": activity},
            )
    except IntegrityError as exc:
        raise AccountError("STATION_EXISTS", "A station with that name already exists.") from exc
    write_audit(conn, "STATION_CREATED", operator_id=actor_id, station_id=station_id, venue_id=venue_id,
                details={"activity": activity})


def _lock(conn: Connection, station_id: str, venue_id: Optional[str]) -> dict:
    row = conn.execute(
        text("SELECT station_id, venue_id, activity, active FROM stations WHERE station_id = :s FOR UPDATE"),
        {"s": station_id},
    ).mappings().one_or_none()
    if row is None:
        raise AccountError("STATION_NOT_FOUND", "That station does not exist.", 404)
    if venue_id is not None and row["venue_id"] != venue_id:
        raise AccountError(
            "WRONG_VENUE",
            f"That station belongs to the {ownership.VENUE_LABEL[row['venue_id']]} server, not here.",
            403,
        )
    return dict(row)


def station_for_device(conn: Connection, device_token: Optional[str], venue_id: Optional[str]) -> Optional[dict]:
    """The active station of THIS venue that this laptop is bound to, or None."""
    if not device_token or venue_id is None:
        return None
    row = conn.execute(
        text(
            "SELECT station_id, venue_id, activity FROM stations "
            "WHERE device_token_hash = :h AND venue_id = :v AND active"
        ),
        {"h": hash_token(device_token), "v": venue_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def bind_station(
    conn: Connection,
    station_id: str,
    *,
    actor_id,
    previous_device_token: Optional[str] = None,
    venue_id: Optional[str] = None,
) -> str:
    """Make the calling laptop THIS station. Returns the new device token to store on the laptop.

    * a fresh token replaces the old one, so the old laptop stops working;
    * whoever was signed in at this station is signed out;
    * if the laptop was bound to another station, that station is freed (one laptop, one station).
    """
    station = _lock(conn, station_id, venue_id)
    if not station["active"]:
        raise AccountError("STATION_INACTIVE", "That station is switched off.")

    if previous_device_token:
        moved_from = conn.execute(
            text("SELECT station_id FROM stations WHERE device_token_hash = :h FOR UPDATE"),
            {"h": hash_token(previous_device_token)},
        ).scalar()
        if moved_from and moved_from != station_id:
            _clear_binding(conn, moved_from, actor_id=actor_id, venue_id=station["venue_id"], reason="laptop moved")

    token = secrets.token_urlsafe(32)
    replaced = conn.execute(
        text("SELECT device_token_hash IS NOT NULL FROM stations WHERE station_id = :s"), {"s": station_id}
    ).scalar()
    revoke_station_sessions(conn, station_id)
    conn.execute(
        text(
            "UPDATE stations SET device_token_hash = :h, bound_at = now(), bound_by = :u WHERE station_id = :s"
        ),
        {"h": hash_token(token), "u": actor_id, "s": station_id},
    )
    write_audit(conn, "STATION_BOUND", operator_id=actor_id, station_id=station_id, venue_id=station["venue_id"],
                details={"replaced_previous_laptop": bool(replaced)})
    return token


def _clear_binding(conn: Connection, station_id: str, *, actor_id, venue_id: str, reason: str) -> None:
    revoke_station_sessions(conn, station_id)
    conn.execute(
        text("UPDATE stations SET device_token_hash = NULL, bound_at = NULL, bound_by = NULL WHERE station_id = :s"),
        {"s": station_id},
    )
    write_audit(conn, "STATION_UNBOUND", operator_id=actor_id, station_id=station_id, venue_id=venue_id,
                details={"reason": reason})


def unbind_station(conn: Connection, station_id: str, *, actor_id, venue_id: Optional[str] = None) -> None:
    station = _lock(conn, station_id, venue_id)
    _clear_binding(conn, station_id, actor_id=actor_id, venue_id=station["venue_id"], reason="unbound by admin")


def set_station_active(
    conn: Connection, station_id: str, active: bool, *, actor_id, venue_id: Optional[str] = None
) -> None:
    station = _lock(conn, station_id, venue_id)
    conn.execute(text("UPDATE stations SET active = :a WHERE station_id = :s"), {"a": active, "s": station_id})
    if not active:
        revoke_station_sessions(conn, station_id)
    write_audit(conn, "STATION_ACTIVATED" if active else "STATION_DEACTIVATED", operator_id=actor_id,
                station_id=station_id, venue_id=station["venue_id"])


def list_stations(conn: Connection, venue_id: str) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT station_id, activity, active, device_token_hash IS NOT NULL AS bound, bound_at "
            "FROM stations WHERE venue_id = :v ORDER BY activity, station_id"
        ),
        {"v": venue_id},
    ).mappings()
    return [dict(r) for r in rows]

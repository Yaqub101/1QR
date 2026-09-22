"""Server-side sessions.

Only a SHA-256 of the random token is stored, so a copy of the database does not
hand out live sessions. Validity is decided in ONE query against the current
state of the user, which is why disabling a user cuts them off on the very next
request. All times are the database server's clock, never the laptop's
(SYSTEM_SPEC 9.7).

A session no longer belongs to a bound station (docs/ARCHITECTURE_PIVOT.md): any
signed-in operator can act from any browser, and their role alone decides which
activity they may perform.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.security import permissions

TOUCH_EVERY_SECONDS = 30  # renew the idle timer at most this often, to spare the database


@dataclass(frozen=True)
class Principal:
    session_id: str
    user_id: object
    username: str
    full_name: Optional[str]
    role: str

    @property
    def is_admin(self) -> bool:
        return permissions.is_admin_role(self.role)

    @property
    def permissions(self) -> frozenset:
        return permissions.permissions_for(self.role)


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn: Connection, *, user_id, max_hours: int) -> str:
    # Housekeeping: forget sessions that ended more than a day ago.
    conn.execute(
        text(
            "DELETE FROM sessions WHERE expires_at < now() - interval '1 day' "
            "OR revoked_at < now() - interval '1 day'"
        )
    )
    token = new_token()
    conn.execute(
        text(
            "INSERT INTO sessions (token_hash, user_id, expires_at) "
            "VALUES (:h, :u, now() + make_interval(hours => :hours))"
        ),
        {"h": hash_token(token), "u": user_id, "hours": max_hours},
    )
    return token


def load_principal(conn: Connection, token: str, *, idle_minutes: int) -> Optional[Principal]:
    row = conn.execute(
        text(
            """
            SELECT s.id AS session_id, s.user_id, u.username, u.full_name, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id AND u.active
            WHERE s.token_hash = :h
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND s.last_seen_at > now() - make_interval(mins => :idle)
            """
        ),
        {"h": hash_token(token), "idle": idle_minutes},
    ).mappings().one_or_none()
    if row is None:
        return None
    conn.execute(
        text(
            "UPDATE sessions SET last_seen_at = now() "
            "WHERE id = :id AND last_seen_at < now() - make_interval(secs => :s)"
        ),
        {"id": row["session_id"], "s": TOUCH_EVERY_SECONDS},
    )
    return Principal(
        session_id=str(row["session_id"]),
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        role=row["role"],
    )


def session_existed(conn: Connection, token: str) -> bool:
    """True if this token was once a session (so a failed check means 'ended', not 'never signed in')."""
    return (
        conn.execute(text("SELECT 1 FROM sessions WHERE token_hash = :h"), {"h": hash_token(token)}).scalar()
        is not None
    )


def revoke_token(conn: Connection, token: str) -> None:
    conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL"),
        {"h": hash_token(token)},
    )


def revoke_user_sessions(conn: Connection, user_id) -> None:
    conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"), {"u": user_id}
    )

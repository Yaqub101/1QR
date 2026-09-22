"""User accounts (Phase 5). Every message on AccountError is one plain sentence."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit
from backend.security import passwords, permissions
from backend.security.sessions import revoke_user_sessions

MAX_USERNAME_LENGTH = 64


class AccountError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def create_user(
    conn: Connection,
    *,
    username: str,
    password: str,
    role: str,
    full_name: Optional[str] = None,
    actor_id=None,
    audit_action: str = "USER_CREATED",
) -> uuid.UUID:
    username = (username or "").strip()
    if not username:
        raise AccountError("BAD_USERNAME", "The username cannot be blank.")
    if len(username) > MAX_USERNAME_LENGTH:
        raise AccountError("BAD_USERNAME", f"The username can be at most {MAX_USERNAME_LENGTH} characters.")
    if role not in permissions.ALL_ROLES:
        raise AccountError("BAD_ROLE", "That role does not exist.")
    try:
        passwords.validate_password(password, role)
    except passwords.WeakPasswordError as exc:
        raise AccountError("WEAK_PASSWORD", exc.message) from exc

    try:
        with conn.begin_nested():
            user_id = conn.execute(
                text(
                    "INSERT INTO users (username, password_hash, full_name, role) "
                    "VALUES (:u, :h, :n, :r) RETURNING id"
                ),
                {"u": username, "h": passwords.hash_password(password), "n": (full_name or "").strip() or None, "r": role},
            ).scalar_one()
    except IntegrityError as exc:
        raise AccountError("USERNAME_TAKEN", "That username is already in use.") from exc
    write_audit(conn, audit_action, operator_id=actor_id, details={"username": username, "role": role, "user_id": user_id})
    return user_id


def _get(conn: Connection, user_id) -> dict:
    row = conn.execute(
        text("SELECT id, username, role, active FROM users WHERE id = :i FOR UPDATE"), {"i": user_id}
    ).mappings().one_or_none()
    if row is None:
        raise AccountError("USER_NOT_FOUND", "That user does not exist.", 404)
    return dict(row)


def set_user_active(conn: Connection, user_id, active: bool, *, actor_id) -> None:
    if not active and str(user_id) == str(actor_id):
        raise AccountError("SELF_DEACTIVATE", "You cannot switch off your own account.")
    user = _get(conn, user_id)
    if not active and user["role"] in ("ADMIN", "DEPUTY_ADMIN"):
        count = conn.execute(text("SELECT count(*) FROM users WHERE role IN ('ADMIN', 'DEPUTY_ADMIN') AND active = :act"), {"act": True}).scalar()
        if count <= 1:
            raise AccountError("LAST_ADMIN", "You cannot switch off the last remaining Admin/Deputy account.")
    conn.execute(text("UPDATE users SET active = :a WHERE id = :i"), {"a": active, "i": user_id})
    if not active:
        revoke_user_sessions(conn, user_id)
    write_audit(
        conn,
        "USER_ACTIVATED" if active else "USER_DEACTIVATED",
        operator_id=actor_id,
        details={"username": user["username"], "user_id": user_id},
    )


def delete_user(conn: Connection, user_id, *, actor_id) -> None:
    user = _get(conn, user_id)
    if str(user_id) == str(actor_id):
        raise AccountError("SELF_DELETE", "You cannot delete your own account.")
    if user["active"] and user["role"] in ("ADMIN", "DEPUTY_ADMIN"):
        count = conn.execute(text("SELECT count(*) FROM users WHERE role IN ('ADMIN', 'DEPUTY_ADMIN') AND active = :act"), {"act": True}).scalar()
        if count <= 1:
            raise AccountError("LAST_ADMIN", "You cannot delete the last remaining Admin/Deputy account.")

    try:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
    except IntegrityError as exc:
        raise AccountError("USER_HAS_ACTIVITY", "Cannot delete this user because they have recorded activity in the system. Switch the account off instead.") from exc

    revoke_user_sessions(conn, user_id)
    write_audit(
        conn,
        "USER_DELETED",
        operator_id=actor_id,
        details={"username": user["username"], "user_id": user_id, "role": user["role"]},
    )


def reset_password(conn: Connection, user_id, new_password: str, *, actor_id) -> None:
    user = _get(conn, user_id)
    try:
        passwords.validate_password(new_password, user["role"])
    except passwords.WeakPasswordError as exc:
        raise AccountError("WEAK_PASSWORD", exc.message) from exc
    conn.execute(
        text("UPDATE users SET password_hash = :h WHERE id = :i"),
        {"h": passwords.hash_password(new_password), "i": user_id},
    )
    revoke_user_sessions(conn, user_id)  # whoever had the old password is signed out
    write_audit(conn, "PASSWORD_RESET", operator_id=actor_id, details={"username": user["username"], "user_id": user_id})


def list_users(conn: Connection) -> list[dict]:
    rows = conn.execute(
        text("SELECT id, username, full_name, role, active, created_at FROM users ORDER BY role, lower(username)")
    ).mappings()
    return [dict(r) for r in rows]

"""Login: credentials -> session.

Expected failures are returned, not raised, so the caller can COMMIT the audit row
that records them before answering. Every message is one plain sentence
(golden rule 11).

Any active account, Admin or operator, can sign in from any browser (docs/
ARCHITECTURE_PIVOT.md): there is no station binding to check any more, and no
"which venue is this" gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.audit import write_audit
from backend.security import passwords
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


def _fail(status: int, code: str, message: str) -> LoginResult:
    return LoginResult(error=LoginError(status, code, message))


BAD_CREDENTIALS = ("BAD_CREDENTIALS", "Wrong username or password.")


def attempt_login(conn: Connection, *, settings, username: str, password: str) -> LoginResult:
    user = conn.execute(
        text("SELECT id, username, full_name, role, active, password_hash FROM users WHERE lower(username) = lower(:u)"),
        {"u": (username or "").strip()},
    ).mappings().one_or_none()

    if user is None:
        passwords.verify_against_dummy(password or "")
        write_audit(conn, "LOGIN_FAILED", details={"reason": "unknown user"})
        return _fail(401, *BAD_CREDENTIALS)

    if not passwords.verify_password(password or "", user["password_hash"]):
        write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], details={"reason": "wrong password"})
        return _fail(401, *BAD_CREDENTIALS)

    # Only someone who proved the password is told the account is off.
    if not user["active"]:
        write_audit(conn, "LOGIN_FAILED", operator_id=user["id"], details={"reason": "account disabled"})
        return _fail(403, "ACCOUNT_DISABLED", "This account is switched off. Please call the Admin.")

    token = create_session(conn, user_id=user["id"], max_hours=settings.session_max_hours)
    write_audit(conn, "LOGIN", operator_id=user["id"])
    return LoginResult(
        token=token, user_id=user["id"], username=user["username"], full_name=user["full_name"], role=user["role"],
    )

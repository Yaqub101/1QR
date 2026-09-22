"""QR tokens (SYSTEM_SPEC 6 and 20; AGENTS.md golden rule 1).

THE RULE THIS MODULE EXISTS TO KEEP: the QR holds one opaque random token and NOTHING else. The token is 16 bytes
(128 bits) straight from `secrets.token_bytes`, written as 32 upper-case hex characters. It is not derived from the
PRN, the name, a counter or a clock, so it says nothing about the student and cannot be guessed from another one.
Upper-case hex is also the friendliest thing for a keyboard-style USB scanner (digits and A-F only: nothing that
changes with the keyboard layout) and lets the QR use its compact alphanumeric mode.

Two operations, both safe to repeat or interrupt:

  generate_missing_tokens   gives every ACTIVE student who has no active token exactly one. Running it again does
                            nothing: existing tokens are never read back, rewritten or replaced. The database's
                            partial unique index (one ACTIVE token per student) is the backstop, so two Admins pressing
                            the button at once still leave one token each.
  reissue_token             the Admin's "Reissue QR": the old token becomes NOT ACTIVE (deactivated_at/by set, kept
                            forever, never rewritten: migration 0003 forbids it) and a new active one is created, with a
                            mandatory reason, in ONE transaction together with its audit row.

Neither writes a token VALUE anywhere but qr_tokens: the audit log and the API answers carry token ids, never the
secret (an exported audit log must not double as a list of live passes).
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from backend.audit import write_audit

logger = logging.getLogger("backend.admin")

TOKEN_BYTES = 16          # 128 bits: the minimum SYSTEM_SPEC 6 allows
MAX_REASON_LENGTH = 500
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class TokenError(Exception):
    """A refusal with an HTTP status. Nothing was written. `message` is one plain sentence."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


@dataclass(frozen=True)
class GenerateResult:
    created: int          # tokens issued by THIS run
    active_students: int  # ACTIVE students on the master list
    with_token: int       # of those, how many now hold an active token (equal to active_students afterwards)


@dataclass(frozen=True)
class ReissueResult:
    student_id: str
    old_token_id: Optional[int]   # None when the student had no active token to replace
    new_token_id: int


def is_student_id(value) -> bool:
    """True if `value` looks like a student id (a UUID). Lets a route answer a malformed id with a plain 404."""
    return bool(_UUID.match(str(value or "")))


def new_token() -> str:
    """32 upper-case hex characters = 128 random bits. Nothing else goes in."""
    return secrets.token_bytes(TOKEN_BYTES).hex().upper()


def clean_reason(reason) -> str:
    """The reason is mandatory and is checked HERE, on the server: blank, whitespace-only or missing is refused."""
    value = " ".join(str(reason or "").split())
    if not value:
        raise TokenError(400, "REASON_REQUIRED", "Please give a reason for reissuing this QR.")
    if len(value) > MAX_REASON_LENGTH:
        raise TokenError(400, "REASON_TOO_LONG", f"Please keep the reason under {MAX_REASON_LENGTH} characters.")
    return value


def generate_missing_tokens(engine, *, operator_id=None, venue_id: Optional[str] = None) -> GenerateResult:
    """Issue one active token to every ACTIVE student who has none. Idempotent: a second run creates nothing, touches
    no existing row and writes no audit entry (a run that did nothing leaves no trace)."""
    with engine.begin() as conn:
        missing = conn.execute(text(
            "SELECT s.id FROM students s WHERE s.status = 'ACTIVE' "
            "AND NOT EXISTS (SELECT 1 FROM qr_tokens t WHERE t.student_id = s.id AND t.active) "
            "ORDER BY s.id")).scalars().all()          # a fixed order, so two concurrent runs cannot deadlock
        created = 0
        for student_id in missing:
            # ON CONFLICT on the one-active-token-per-student index: if another run got there first, this is a no-op.
            # A clash on the token value itself (2^-128) is NOT swallowed: it would raise, loudly.
            created += conn.execute(
                text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t) ON CONFLICT (student_id) WHERE active DO NOTHING"),
                {"s": student_id, "t": new_token()}).rowcount
        if created:
            write_audit(conn, "QR_TOKENS_GENERATED", operator_id=operator_id, venue_id=venue_id, details={"created": created})
        active = conn.execute(text("SELECT count(*) FROM students WHERE status = 'ACTIVE'")).scalar_one()
        held = conn.execute(text(
            "SELECT count(*) FROM students s WHERE s.status = 'ACTIVE' "
            "AND EXISTS (SELECT 1 FROM qr_tokens t WHERE t.student_id = s.id AND t.active)")).scalar_one()
    if created:
        logger.info("generated %d QR tokens", created)
    return GenerateResult(created=created, active_students=int(active), with_token=int(held))


def reissue_token(engine, *, student_id, reason, operator_id, venue_id: Optional[str] = None) -> ReissueResult:
    """Admin "Reissue QR". The old token stops working (NOT ACTIVE) and a new one is issued, or nothing changes at all.

    The student's row is locked first, so two simultaneous reissues run one after the other and end with exactly one
    active token; and the token change and its audit row commit together or not at all."""
    reason = clean_reason(reason)
    if not is_student_id(student_id):
        raise TokenError(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    with engine.begin() as conn:
        student = conn.execute(text("SELECT id, prn, status FROM students WHERE id = :i FOR UPDATE"), {"i": str(student_id)}).mappings().one_or_none()
        if student is None:
            raise TokenError(404, "STUDENT_NOT_FOUND", "That student does not exist.")
        if student["status"] != "ACTIVE":
            raise TokenError(409, "STUDENT_NOT_ACTIVE", "That student is not active, so a QR cannot be issued.")
        old_id = conn.execute(
            text("UPDATE qr_tokens SET active = false, deactivated_at = now(), deactivated_by = :u "
                 "WHERE student_id = :s AND active RETURNING id"),
            {"u": operator_id, "s": student["id"]}).scalar_one_or_none()
        new_id = conn.execute(
            text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t) RETURNING id"),
            {"s": student["id"], "t": new_token()}).scalar_one()
        write_audit(conn, "QR_REISSUED", operator_id=operator_id, venue_id=venue_id, reason=reason, student_id=student["id"],
                    details={"prn": student["prn"], "old_token_id": old_id, "new_token_id": new_id})
    logger.info("QR reissued for student %s (token %s -> %s)", student["id"], old_id, new_id)
    return ReissueResult(student_id=str(student["id"]), old_token_id=old_id, new_token_id=new_id)


def status_for(conn, student_id) -> dict:
    """For the Admin's student page: does this student hold an active QR, and how many have been issued?"""
    row = conn.execute(text(
        "SELECT count(*) AS issued, count(*) FILTER (WHERE active) AS active FROM qr_tokens WHERE student_id = :s"),
        {"s": student_id}).mappings().one()
    return {"has_active": row["active"] == 1, "issued": row["issued"], "reissued": max(row["issued"] - 1, 0)}

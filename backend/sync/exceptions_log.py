"""Raising and closing Admin exceptions from sync, reconciliation and the station engine.

Raising is IDEMPOTENT: the partial unique indexes from migration 0008 allow one OPEN item per (type, event)
and one OPEN gap per (venue, first missing number), so running reconciliation twice, or receiving the same
batch twice, never piles up duplicates. Closing an item by the system (its cause went away) leaves
resolved_by empty and says why in the note; an Admin's own resolution is still the Phase 13 action.

No engine imports here on purpose: the station engine calls into this module.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit

UNIQUE_VIOLATION = "23505"


def open_exception(conn: Connection, type_: str, *, student_id: Any = None, venue_id: Optional[str] = None,
                   event_id: Any = None, details: Optional[dict] = None) -> bool:
    """Raise an OPEN exception unless an identical one is already open. True when a new one was raised."""
    try:
        with conn.begin_nested():
            conn.execute(
                text("INSERT INTO exceptions (type, student_id, venue_id, event_id, details) "
                     "VALUES (:t, :s, :v, :e, CAST(:d AS jsonb))"),
                {"t": type_, "s": student_id, "v": venue_id, "e": event_id, "d": json.dumps(details or {}, default=str)},
            )
    except IntegrityError as exc:
        if getattr(exc.orig, "pgcode", None) == UNIQUE_VIOLATION:
            return False
        raise
    return True


def close_exception(conn: Connection, exception_id: int, note: str) -> bool:
    """Close an OPEN exception because the problem resolved itself (e.g. the missing record arrived)."""
    row = conn.execute(
        text("UPDATE exceptions SET status = 'RESOLVED', resolved_at = now(), resolution_note = :n "
             "WHERE id = :i AND status = 'OPEN' RETURNING type, student_id, venue_id, event_id"),
        {"i": exception_id, "n": note},
    ).mappings().one_or_none()
    if row is None:
        return False
    write_audit(conn, "EXCEPTION_AUTO_CLOSED", venue_id=row["venue_id"], reason=note, student_id=row["student_id"],
                event_id=row["event_id"], details={"exception_id": exception_id, "type": row["type"]})
    return True

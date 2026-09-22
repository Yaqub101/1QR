"""Raising and closing Admin exceptions from system code (not from an Admin's own action).

Before the single-server pivot (docs/ARCHITECTURE_PIVOT.md) this was called by the sync/reconciliation
engine (for CONFLICT, SEQ_GAP, PROVISIONAL_UNCONFIRMED) and by the station engine (PROVISIONAL_UNCONFIRMED).
Both of those are gone along with the venue/sync layer, so nothing in the application currently calls
`open_exception`/`close_exception` -- they are kept as generic, reusable infrastructure for the next
exception type that needs to be raised by system code rather than by an Admin's own action (e.g. RETURN_WAIVED
in backend/admin/corrections.py, which inserts its row directly since it is raised alongside a correction
already being written in the same transaction).

Closing an item by the system (its cause went away) leaves resolved_by empty and says why in the note;
an Admin's own resolution is still the Phase 13 action.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.audit import write_audit


def open_exception(conn: Connection, type_: str, *, student_id: Any = None,
                   event_id: Any = None, details: Optional[dict] = None) -> None:
    """Raise an OPEN exception."""
    conn.execute(
        text("INSERT INTO exceptions (type, student_id, event_id, details) "
             "VALUES (:t, :s, :e, CAST(:d AS jsonb))"),
        {"t": type_, "s": student_id, "e": event_id, "d": json.dumps(details or {}, default=str)},
    )


def close_exception(conn: Connection, exception_id: int, note: str) -> bool:
    """Close an OPEN exception because the problem resolved itself."""
    row = conn.execute(
        text("UPDATE exceptions SET status = 'RESOLVED', resolved_at = now(), resolution_note = :n "
             "WHERE id = :i AND status = 'OPEN' RETURNING type, student_id, event_id"),
        {"i": exception_id, "n": note},
    ).mappings().one_or_none()
    if row is None:
        return False
    write_audit(conn, "EXCEPTION_AUTO_CLOSED", reason=note, student_id=row["student_id"],
                event_id=row["event_id"], details={"exception_id": exception_id, "type": row["type"]})
    return True

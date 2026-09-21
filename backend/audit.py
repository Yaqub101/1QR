"""Tiny helper for writing audit_log rows (append-only; SYSTEM_SPEC section 17)."""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection


def write_audit(
    conn: Connection,
    action: str,
    *,
    operator_id: Any = None,
    station_id: Optional[str] = None,
    venue_id: Optional[str] = None,
    reason: Optional[str] = None,
    details: Optional[dict] = None,
    student_id: Any = None,
    activity: Optional[str] = None,
    event_id: Any = None,
    venue_seq: Optional[int] = None,
    flags: Sequence[str] = (),
) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (action, operator_id, station_id, venue_id, reason, details, "
            "student_id, activity, event_id, venue_seq, flags) "
            "VALUES (:action, :operator_id, :station_id, :venue_id, :reason, CAST(:details AS jsonb), "
            ":student_id, :activity, :event_id, :venue_seq, CAST(:flags AS text[]))"
        ),
        {
            "action": action,
            "operator_id": operator_id,
            "station_id": station_id,
            "venue_id": venue_id,
            "reason": reason,
            "details": json.dumps(details or {}, default=str),
            "student_id": student_id,
            "activity": activity,
            "event_id": event_id,
            "venue_seq": venue_seq,
            "flags": list(flags),
        },
    )

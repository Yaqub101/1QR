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
    reason: Optional[str] = None,
    details: Optional[dict] = None,
    student_id: Any = None,
    activity: Optional[str] = None,
    event_id: Any = None,
    flags: Sequence[str] = (),
    corrects_event_id: Any = None,
    corrected_by: Any = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (action, operator_id, reason, details, "
            "student_id, activity, event_id, flags, corrects_event_id, corrected_by) "
            "VALUES (:action, :operator_id, :reason, CAST(:details AS jsonb), "
            ":student_id, :activity, :event_id, CAST(:flags AS text[]), :corrects, :corrected_by)"
        ),
        {
            "action": action,
            "operator_id": operator_id,
            "reason": reason,
            "details": json.dumps(details or {}, default=str),
            "student_id": student_id,
            "activity": activity,
            "event_id": event_id,
            "flags": list(flags),
            "corrects": corrects_event_id,
            "corrected_by": corrected_by,
        },
    )

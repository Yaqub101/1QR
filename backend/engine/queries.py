"""The few reads the engine needs. An 'active completion' is a COMPLETE or WAIVER event that has not
been reversed; it is exactly the definition the student_status view and the database's unique index use."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

ACTIVE_COMPLETION_SQL = """
    SELECT e.event_id, e.kind, e.server_time, e.station_id, e.details, e.completion_cycle
    FROM activity_events e
    WHERE e.student_id = :student_id AND e.activity = :activity AND e.kind IN ('COMPLETE','WAIVER')
      AND NOT EXISTS (
          SELECT 1 FROM activity_events r
          WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id AND r.activity = e.activity
            AND r.completion_cycle = e.completion_cycle)
    ORDER BY e.completion_cycle DESC, e.venue_seq DESC
    LIMIT 1
"""


def active_completion(conn: Connection, student_id, activity: str) -> Optional[dict]:
    row = conn.execute(text(ACTIVE_COMPLETION_SQL), {"student_id": student_id, "activity": activity}).mappings().first()
    return dict(row) if row else None


def next_completion_cycle(conn: Connection, student_id, activity: str) -> int:
    """1 for the first completion; one more for each Admin reversal since (see migration 0002)."""
    reversals = conn.execute(
        text("SELECT count(*) FROM activity_events WHERE kind = 'REVERSAL' AND student_id = :s AND activity = :a"),
        {"s": student_id, "a": activity},
    ).scalar_one()
    return int(reversals) + 1


def clock_text(when: datetime, offset_minutes: int) -> str:
    """'11:21 AM' on the event clock (a fixed offset: India has no daylight saving). Server time only."""
    local = when.astimezone(timezone(timedelta(minutes=offset_minutes)))
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"

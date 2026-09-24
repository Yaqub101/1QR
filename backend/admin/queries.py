"""The definitions the dashboard AND the reports share, so the two can never disagree.

An ACTIVE completion is a COMPLETE or WAIVER event that has no REVERSAL for its (student, activity, cycle):
the same definition as the student_status view (migration 0004) and backend/engine/queries.py. A student has
at most one active completion per activity (the Phase 2 unique index), so counting rows in `active` counts
students.

Population: every row of `students` (the university master list), whatever its status, so every figure
reconciles to the master count. "Reported" = an active Reporting; "Not Attended" = NO Reporting event
of any kind (SYSTEM_SPEC C3); a student whose Reporting was reversed is neither (they are "Yet to Report").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.security.ownership import ACTIVITIES

ACTIVE_CTE = """
active AS (
    SELECT e.*
    FROM activity_events e
    WHERE e.kind IN ('COMPLETE','WAIVER')
      AND NOT EXISTS (
          SELECT 1 FROM activity_events r
          WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id AND r.activity = e.activity
            AND r.completion_cycle = e.completion_cycle)
)"""

NEVER_REGISTERED = """
NOT EXISTS (SELECT 1 FROM activity_events e WHERE e.student_id = s.id AND e.activity = 'REGISTRATION')"""

FUNNEL_LABELS = {
    "REGISTRATION": "Reported", "THOBE_ALLOCATION": "Robe received", "MONEY_RECEIVED": "Money received",
    "SEATING": "Seated (optional)", "QUEUE": "Queued", "STAGE": "Stage complete", "THOBE_RETURN": "Robe returned",
    "MONEY_RETURNED": "Money returned", "LUNCH": "Lunch / Exited",
}
assert tuple(FUNNEL_LABELS) == ACTIVITIES


def journey_status(label: str) -> str:
    """The student_status view's label ("REPORTED / MONEY PENDING") as the Admin screens show it. The view is the
    ONE place the labels are defined; this only changes the capitals."""
    return label.capitalize() if label else ""


def event_clock(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=offset_minutes))


def iso_local(value: Optional[datetime], offset_minutes: int) -> Optional[str]:
    """A server timestamp as ISO-8601 on the event clock ('2026-10-15T11:21:05+05:30'): unambiguous in a spreadsheet."""
    if value is None:
        return None
    return value.astimezone(event_clock(offset_minutes)).replace(microsecond=0).isoformat()


def percent(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0

"""The Admin audit viewer and its export source (SYSTEM_SPEC 17). Read only: the audit log is append-only."""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin.queries import iso_local

MAX_PAGE = 1000

COLUMNS = [
    ("id", "Audit id"), ("occurred_at", "Server time"), ("local_time", "Operator laptop time"), ("action", "Action"),
    ("prn", "PRN"), ("student", "Student"), ("activity", "Activity"),
    ("operator", "Operator"), ("event_id", "Event id"), ("flags", "Flags"),
    ("corrects_event_id", "Corrects event"), ("corrected_by", "Corrected by (Admin)"), ("reason", "Reason"),
    ("details", "Details"),
]

_FROM = """
    FROM audit_log a
    LEFT JOIN students s ON s.id = a.student_id
    LEFT JOIN users op ON op.id = a.operator_id
    LEFT JOIN users cb ON cb.id = a.corrected_by
"""


def _filters(*, student: Optional[str], action: Optional[str], activity: Optional[str], operator: Optional[str],
             since: Optional[str], until: Optional[str]) -> tuple[str, dict]:
    where, p = ["true"], {}
    if student:
        where.append("(s.prn ILIKE :student OR s.name ILIKE :student)")
        p["student"] = f"%{student.strip()}%".replace("\\", "\\\\")
    if action:
        where.append("a.action = :action")
        p["action"] = action.strip()
    if activity:
        where.append("a.activity = :activity")
        p["activity"] = activity.strip().upper()
    if operator:
        where.append("(op.username ILIKE :operator OR cb.username ILIKE :operator)")
        p["operator"] = f"%{operator.strip()}%"
    if since:
        where.append("a.occurred_at >= CAST(:since AS timestamptz)")
        p["since"] = since
    if until:
        where.append("a.occurred_at <= CAST(:until AS timestamptz)")
        p["until"] = until
    return " AND ".join(where), p


def _row(r, off: int) -> dict:
    return {
        "id": r["id"], "occurred_at": iso_local(r["occurred_at"], off), "local_time": iso_local(r["local_time"], off),
        "action": r["action"], "prn": r["prn"], "student": r["student"], "activity": r["activity"],
        "operator": r["operator"], "event_id": str(r["event_id"]) if r["event_id"] else None,
        "flags": ", ".join(r["flags"] or []),
        "corrects_event_id": str(r["corrects_event_id"]) if r["corrects_event_id"] else None,
        "corrected_by": r["corrected_by_name"], "reason": r["reason"],
        "details": json.dumps(r["details"], ensure_ascii=False, sort_keys=True) if r["details"] else "",
    }


_SELECT = ("SELECT a.id, a.occurred_at, a.local_time, a.action, s.prn, s.name AS student, a.activity, "
           "op.username AS operator, a.event_id, a.flags, a.corrects_event_id, cb.username AS corrected_by_name, "
           "a.reason, a.details")


def list_audit(conn: Connection, settings, *, student=None, action=None, activity=None, operator=None, since=None,
               until=None, limit: int = 100, offset: int = 0) -> dict:
    clause, params = _filters(student=student, action=action, activity=activity, operator=operator, since=since, until=until)
    total = int(conn.execute(text(f"SELECT count(*) {_FROM} WHERE {clause}"), params).scalar_one())
    rows = conn.execute(text(f"{_SELECT} {_FROM} WHERE {clause} ORDER BY a.id DESC LIMIT :n OFFSET :o"),
                        {**params, "n": max(1, min(limit, MAX_PAGE)), "o": max(0, offset)}).mappings()
    off = settings.event_utc_offset_minutes
    return {"total": total, "rows": [_row(r, off) for r in rows]}


def all_audit(conn: Connection, settings, **filters) -> list[dict]:
    """Every matching row, oldest first, for the export."""
    clause, params = _filters(**{k: filters.get(k) for k in ("student", "action", "activity", "operator", "since", "until")})
    off = settings.event_utc_offset_minutes
    return [_row(r, off) for r in conn.execute(text(f"{_SELECT} {_FROM} WHERE {clause} ORDER BY a.id"), params).mappings()]


def actions(conn: Connection) -> list[str]:
    return [r[0] for r in conn.execute(text("SELECT DISTINCT action FROM audit_log ORDER BY action"))]

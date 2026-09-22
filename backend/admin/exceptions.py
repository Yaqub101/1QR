"""The Admin exception list and its resolve action (SYSTEM_SPEC 11.7, 12).

Resolving records who reviewed an item and why; it does not undo anything (a waiver stays a waiver: reverse it
from the student's journey if it was a mistake). A resolved item is final: migration 0007 makes the row
un-editable and un-deletable, so a resolution note can never be overwritten.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin.corrections import CorrectionError, _require_admin
from backend.admin.queries import iso_local
from backend.audit import write_audit

PAGE = 200


def list_exceptions(conn: Connection, settings, *, status: Optional[str] = None, type_: Optional[str] = None,
                    limit: int = PAGE, offset: int = 0) -> dict:
    where, params = ["true"], {"n": max(1, min(limit, 1000)), "o": max(0, offset)}
    if status in ("OPEN", "RESOLVED"):
        where.append("x.status = :status")
        params["status"] = status
    if type_:
        where.append("x.type = :type")
        params["type"] = type_
    clause = " AND ".join(where)
    total = int(conn.execute(text(f"SELECT count(*) FROM exceptions x WHERE {clause}"), params).scalar_one())
    rows = conn.execute(text(
        f"SELECT x.id, x.type, x.status, x.created_at, x.event_id, x.details, x.resolved_at, x.resolution_note, "
        f"s.id AS student_id, s.prn, s.name, ru.username AS resolved_by "
        f"FROM exceptions x LEFT JOIN students s ON s.id = x.student_id LEFT JOIN users ru ON ru.id = x.resolved_by "
        f"WHERE {clause} ORDER BY (x.status = 'OPEN') DESC, x.created_at DESC, x.id DESC LIMIT :n OFFSET :o"), params).mappings()
    off = settings.event_utc_offset_minutes
    return {"total": total, "exceptions": [
        {"id": r["id"], "type": r["type"], "status": r["status"], "created_at": iso_local(r["created_at"], off),
         "event_id": str(r["event_id"]) if r["event_id"] else None,
         "student_id": str(r["student_id"]) if r["student_id"] else None, "prn": r["prn"], "name": r["name"],
         "details": r["details"], "reason": (r["details"] or {}).get("reason"), "resolved_by": r["resolved_by"],
         "resolved_at": iso_local(r["resolved_at"], off), "resolution_note": r["resolution_note"]} for r in rows]}


def resolve(engine, *, principal, exception_id, note) -> dict:
    """OPEN -> RESOLVED with a mandatory note, and an audit row, in one transaction."""
    _require_admin(principal)
    note = " ".join(str(note or "").split())
    if not note:
        raise CorrectionError(400, "NOTE_REQUIRED", "Please say how this was dealt with.")
    if len(note) > 500:
        raise CorrectionError(400, "NOTE_TOO_LONG", "Please keep the note under 500 characters.")
    try:
        xid = int(exception_id)
    except (TypeError, ValueError):
        raise CorrectionError(404, "EXCEPTION_NOT_FOUND", "That item does not exist.")
    with engine.begin() as conn:
        row = conn.execute(text(
            "UPDATE exceptions SET status = 'RESOLVED', resolved_by = :u, resolved_at = now(), resolution_note = :n "
            "WHERE id = :i AND status = 'OPEN' RETURNING id, type, student_id, event_id"),
            {"u": principal.user_id, "n": note, "i": xid}).mappings().one_or_none()
        if row is None:
            exists = conn.execute(text("SELECT 1 FROM exceptions WHERE id = :i"), {"i": xid}).scalar()
            if not exists:
                raise CorrectionError(404, "EXCEPTION_NOT_FOUND", "That item does not exist.")
            raise CorrectionError(409, "ALREADY_RESOLVED", "That item has already been resolved.")
        write_audit(conn, "EXCEPTION_RESOLVED", operator_id=principal.user_id, reason=note,
                    student_id=row["student_id"], event_id=row["event_id"], details={"exception_id": row["id"], "type": row["type"]})
    return {"ok": True, "message": "Marked as resolved.", "id": xid}

"""Admin student search and the full journey timeline (SYSTEM_SPEC 12, 17). Read only."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin.queries import iso_local, journey_status
from backend.security import ownership

SEARCH_LIMIT = 25


def _like(needle: str) -> str:
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(conn: Connection, query: str = "", limit: int = SEARCH_LIMIT, offset: int = 0) -> dict:
    """By PRN, name or sequence number. Returns a page of results and the total count."""
    needle = " ".join((query or "").split())
    
    where_clause = ""
    params = {"n": limit, "off": offset}
    
    if needle:
        number = int(needle) if needle.isdigit() and len(needle) < 10 else None
        where_clause = "WHERE s.prn ILIKE :like ESCAPE '\\' OR s.name ILIKE :like ESCAPE '\\' OR s.sequence_no = :num "
        params["like"] = _like(needle)
        params["num"] = number
        
    total = conn.execute(text(f"SELECT count(*) FROM students s {where_clause}"), params).scalar()
    
    rows = conn.execute(text(f"""
        SELECT s.id, s.prn, s.name, s.programme, s.school, s.sequence_no, s.status, s.photo_path, v.step, v.status AS journey 
        FROM students s JOIN student_status v ON v.student_id = s.id 
        {where_clause}
        ORDER BY s.sequence_no NULLS LAST, s.name LIMIT :n OFFSET :off"""), params).mappings()
        
    students = [{"student_id": str(r["id"]), "prn": r["prn"], "name": r["name"], "programme": r["programme"],
             "school": r["school"], "sequence_no": r["sequence_no"], "master_status": r["status"],
             "photo_path": r.get("photo_path"),
             "journey_status": journey_status(r["journey"])} for r in rows]
             
    return {"total": total, "students": students}


def journey(conn: Connection, student_id, settings) -> Optional[dict]:
    """The student's master record, derived status and EVERY event in the order it was recorded, each marked
    ACTIVE / REVERSED / CORRECTION / SKIPPED. Nothing here is edited: the timeline is the event log itself."""
    try:
        sid = uuid.UUID(str(student_id))
    except ValueError:
        return None
    off = settings.event_utc_offset_minutes
    student = conn.execute(text(
        "SELECT s.id, s.prn, s.name, s.programme, s.school, s.awards, s.photo_path, s.sequence_no, s.seat_no, s.status, "
        "s.email, s.mobile, "
        "v.step, v.status AS journey, EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) AS frozen "
        "FROM students s JOIN student_status v ON v.student_id = s.id WHERE s.id = :s"), {"s": sid}).mappings().one_or_none()
    if student is None:
        return None
    events = conn.execute(text("""
        SELECT e.event_id, e.activity, e.kind, e.server_time, e.flags,
               e.details, e.completion_cycle, e.corrects_event_id, u.username AS operator,
               rv.event_id AS reversed_by
        FROM activity_events e
        LEFT JOIN users u ON u.id = e.operator_id
        LEFT JOIN activity_events rv ON rv.kind = 'REVERSAL' AND e.kind IN ('COMPLETE','WAIVER')
             AND rv.student_id = e.student_id AND rv.activity = e.activity AND rv.completion_cycle = e.completion_cycle
        WHERE e.student_id = :s ORDER BY e.server_time"""), {"s": sid}).mappings().all()
    timeline = []
    for e in events:
        if e["kind"] in ("COMPLETE", "WAIVER"):
            state = "REVERSED" if e["reversed_by"] else "ACTIVE"
        else:
            state = {"REVERSAL": "CORRECTION", "SKIP": "SKIPPED"}[e["kind"]]
        timeline.append({
            "event_id": str(e["event_id"]), "activity": e["activity"], "activity_label": ownership.ACTIVITY_LABEL[e["activity"]],
            "kind": e["kind"], "state": state,
            "operator": e["operator"], "time": iso_local(e["server_time"], off), "flags": list(e["flags"]),
            "reason": (e["details"] or {}).get("reason"), "details": e["details"],
            "corrects_event_id": str(e["corrects_event_id"]) if e["corrects_event_id"] else None,
            "reversed_by_event_id": str(e["reversed_by"]) if e["reversed_by"] else None,
            "can_reverse": state == "ACTIVE",
        })
    # The attempts panel answers "what went wrong for this student?", so it leaves out the ones that went
    # right: the confirmations, and the READY previews the operator was shown before confirming.
    attempts = conn.execute(text(
        "SELECT occurred_at, activity, result, message FROM scan_log "
        "WHERE student_id = :s AND result NOT IN ('SUCCESS', 'READY') "
        "ORDER BY occurred_at DESC, id DESC LIMIT 50"), {"s": sid}).mappings().all()
    returned = any(t["activity"] == "THOBE_RETURN" and t["state"] == "ACTIVE" for t in timeline)
    money_settled = any(t["activity"] == "MONEY_RETURNED" and t["state"] == "ACTIVE" for t in timeline)
    return {
        # `frozen` says whether "Freeze display data" has been run for this student. Once it has, the
        # master fields below are changed only by a logged master patch (backend/master_patch.py).
        # `seat_no` is carried for the patch form alone: the university supplies no seats, and no
        # screen an operator sees shows one.
        "student": {"student_id": str(student["id"]), "prn": student["prn"], "name": student["name"],
                    "programme": student["programme"], "school": student["school"], "awards": student["awards"],
                    "photo_path": student["photo_path"], "sequence_no": student["sequence_no"],
                    "seat_no": student["seat_no"], "master_status": student["status"],
                    "email": student["email"], "mobile": student["mobile"],
                    "frozen": bool(student["frozen"]),
                    "journey_status": journey_status(student["journey"])},
        "events": timeline,
        "attempts": [{"time": iso_local(a["occurred_at"], off), "activity": a["activity"],
                      "result": a["result"], "message": a["message"]} for a in attempts],
        "can_waive_return": not returned,
        "can_waive_money": False,
    }

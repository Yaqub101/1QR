"""Admin student search and the full journey timeline (SYSTEM_SPEC 12, 17). Read only."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin.queries import STATUS_LABEL, iso_local
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
        SELECT s.id, s.prn, s.name, s.programme, s.school, s.sequence_no, s.status, s.photo_path, v.step 
        FROM students s JOIN student_status v ON v.student_id = s.id 
        {where_clause}
        ORDER BY s.sequence_no NULLS LAST, s.name LIMIT :n OFFSET :off"""), params).mappings()
        
    students = [{"student_id": str(r["id"]), "prn": r["prn"], "name": r["name"], "programme": r["programme"],
             "school": r["school"], "sequence_no": r["sequence_no"], "master_status": r["status"],
             "photo_path": r.get("photo_path"),
             "journey_status": STATUS_LABEL[r["step"]]} for r in rows]
             
    return {"total": total, "students": students}


def journey(conn: Connection, student_id, settings, guard) -> Optional[dict]:
    """The student's master record, derived status and EVERY event in the order it was recorded, each marked
    ACTIVE / REVERSED / CORRECTION / SKIPPED. Nothing here is edited: the timeline is the event log itself."""
    try:
        sid = uuid.UUID(str(student_id))
    except ValueError:
        return None
    off = settings.event_utc_offset_minutes
    student = conn.execute(text(
        "SELECT s.id, s.prn, s.name, s.programme, s.school, s.awards, s.photo_path, s.sequence_no, s.seat_no, s.status, "
        "v.step, EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) AS frozen "
        "FROM students s JOIN student_status v ON v.student_id = s.id WHERE s.id = :s"), {"s": sid}).mappings().one_or_none()
    if student is None:
        return None
    events = conn.execute(text("""
        SELECT e.event_id, e.activity, e.kind, e.venue_id, e.venue_seq, e.station_id, e.server_time, e.flags,
               e.details, e.completion_cycle, e.corrects_event_id, u.username AS operator,
               rv.event_id AS reversed_by, o.sent_at AS synced_at
        FROM activity_events e
        LEFT JOIN users u ON u.id = e.operator_id
        LEFT JOIN outbox o ON o.event_id = e.event_id
        LEFT JOIN activity_events rv ON rv.kind = 'REVERSAL' AND e.kind IN ('COMPLETE','WAIVER')
             AND rv.student_id = e.student_id AND rv.activity = e.activity AND rv.completion_cycle = e.completion_cycle
        WHERE e.student_id = :s ORDER BY e.server_time, e.venue_seq"""), {"s": sid}).mappings().all()
    timeline = []
    for e in events:
        if e["kind"] in ("COMPLETE", "WAIVER"):
            state = "REVERSED" if e["reversed_by"] else "ACTIVE"
        else:
            state = {"REVERSAL": "CORRECTION", "SKIP": "SKIPPED"}[e["kind"]]
        route = guard.route_correction(e["activity"])
        timeline.append({
            "event_id": str(e["event_id"]), "activity": e["activity"], "activity_label": ownership.ACTIVITY_LABEL[e["activity"]],
            "kind": e["kind"], "state": state, "venue": e["venue_id"], "venue_seq": e["venue_seq"], "station_id": e["station_id"],
            "operator": e["operator"], "time": iso_local(e["server_time"], off), "flags": list(e["flags"]),
            "reason": (e["details"] or {}).get("reason"), "details": e["details"],
            "corrects_event_id": str(e["corrects_event_id"]) if e["corrects_event_id"] else None,
            "reversed_by_event_id": str(e["reversed_by"]) if e["reversed_by"] else None,
            "synced_at": iso_local(e["synced_at"], off),
            "can_reverse": state == "ACTIVE", "reverse_here": route.apply_here, "owner_venue": route.owner_venue,
        })
    # The attempts panel answers "what went wrong for this student?", so it leaves out the ones that went
    # right: the confirmations, and the READY previews the operator was shown before confirming.
    attempts = conn.execute(text(
        "SELECT occurred_at, station_id, activity, result, message FROM scan_log "
        "WHERE student_id = :s AND result NOT IN ('SUCCESS', 'READY') "
        "ORDER BY occurred_at DESC, id DESC LIMIT 50"), {"s": sid}).mappings().all()
    returned = any(t["activity"] == "THOBE_RETURN" and t["state"] == "ACTIVE" for t in timeline)
    return {
        # `frozen` says whether "Freeze display data" has been run for this student. Once it has, the
        # master fields below are changed only by a logged master patch (backend/master_patch.py).
        # `seat_no` is carried for the patch form alone: the university supplies no seats, and no
        # screen an operator sees shows one.
        "student": {"student_id": str(student["id"]), "prn": student["prn"], "name": student["name"],
                    "programme": student["programme"], "school": student["school"], "awards": student["awards"],
                    "photo_path": student["photo_path"], "sequence_no": student["sequence_no"],
                    "seat_no": student["seat_no"], "master_status": student["status"],
                    "frozen": bool(student["frozen"]),
                    "journey_status": STATUS_LABEL[student["step"]]},
        "events": timeline,
        "attempts": [{"time": iso_local(a["occurred_at"], off), "station_id": a["station_id"], "activity": a["activity"],
                      "result": a["result"], "message": a["message"]} for a in attempts],
        "can_waive_return": not returned, "waive_here": guard.route_correction("THOBE_RETURN").apply_here,
    }

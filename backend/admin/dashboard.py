"""The live Admin dashboard (SYSTEM_SPEC 12). Read only: it never blocks or changes anything."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.admin.queries import ACTIVE_CTE, FUNNEL_LABELS, NEVER_REGISTERED, iso_local, percent
from backend.security.ownership import ACTIVITIES, ACTIVITY_LABEL
from backend.stage import state as stage_state
from backend.sync import status as sync_status

OUTSTANDING_SHOWN = 50


def counts(conn: Connection) -> dict:
    row = conn.execute(text(f"""
        WITH {ACTIVE_CTE}
        SELECT (SELECT count(*) FROM students)                                        AS registered,
               (SELECT count(*) FROM active WHERE activity = 'REGISTRATION')          AS reported,
               (SELECT count(*) FROM students s WHERE {NEVER_REGISTERED})             AS not_attended,
               (SELECT count(*) FROM students WHERE status = 'INACTIVE')              AS inactive_students
    """)).mappings().one()
    registered, reported = int(row["registered"]), int(row["reported"])
    yet = registered - reported
    return {
        "registered": registered, "reported": reported, "yet_to_report": yet,
        "reporting_percent": percent(reported, registered),
        "not_attended": int(row["not_attended"]),
        "registration_reversed": yet - int(row["not_attended"]),  # registered once, then an Admin reversed it
        "inactive_students": int(row["inactive_students"]),
    }


def school_wise(conn: Connection) -> list[dict]:
    rows = conn.execute(text(f"""
        WITH {ACTIVE_CTE}
        SELECT s.school, count(*) AS registered, count(a.event_id) AS reported
        FROM students s
        LEFT JOIN active a ON a.student_id = s.id AND a.activity = 'REGISTRATION'
        GROUP BY s.school ORDER BY s.school
    """)).mappings()
    return [{"school": r["school"], "registered": int(r["registered"]), "reported": int(r["reported"]),
             "yet_to_report": int(r["registered"]) - int(r["reported"]),
             "reporting_percent": percent(int(r["reported"]), int(r["registered"]))} for r in rows]


def funnel(conn: Connection) -> list[dict]:
    done = {r["activity"]: int(r["n"]) for r in conn.execute(
        text(f"WITH {ACTIVE_CTE} SELECT activity, count(*) AS n FROM active GROUP BY activity")).mappings()}
    waived = int(conn.execute(text(f"WITH {ACTIVE_CTE} SELECT count(*) FROM active WHERE kind = 'WAIVER'")).scalar_one())
    reported = done.get("REGISTRATION", 0)
    return [{"activity": a, "label": FUNNEL_LABELS[a], "count": done.get(a, 0),
             "percent_of_reported": percent(done.get(a, 0), reported),
             **({"of_which_waived": waived} if a == "THOBE_RETURN" else {})} for a in ACTIVITIES]


def stage_view(conn: Connection) -> dict:
    st = stage_state.read_state(conn)
    led = None
    if st["display_student_id"] is not None:
        led = conn.execute(text("SELECT display_name FROM display_snapshot WHERE student_id = :s"),
                           {"s": st["display_student_id"]}).scalar()
    return {
        "current": stage_state.card_for(conn, st["current_student_id"]),
        "led_mode": "SHOWING" if st["display_student_id"] is not None else "HOME",
        "led_name": led,
        "next": stage_state.waiting(conn, 3),
        "waiting": int(conn.execute(text("SELECT count(*) FROM queue WHERE status = 'QUEUED'")).scalar_one()),
    }


OUTSTANDING_FROM = f"""
    WITH {ACTIVE_CTE}
    SELECT s.id AS student_id, s.prn, s.name, s.school, s.programme, al.server_time AS allocated_at,
           EXISTS (SELECT 1 FROM active st WHERE st.student_id = s.id AND st.activity = 'STAGE') AS stage_complete
    FROM active al
    JOIN students s ON s.id = al.student_id
    WHERE al.activity = 'THOBE_ALLOCATION'
      AND NOT EXISTS (SELECT 1 FROM active r WHERE r.student_id = al.student_id AND r.activity = 'THOBE_RETURN')
"""


def outstanding_thobes(conn: Connection, offset_minutes: int, limit: int = OUTSTANDING_SHOWN) -> dict:
    total = int(conn.execute(text(f"SELECT count(*) FROM ({OUTSTANDING_FROM}) o")).scalar_one())
    rows = conn.execute(text(OUTSTANDING_FROM + " ORDER BY al.server_time, s.sequence_no NULLS LAST, s.name LIMIT :n"), {"n": limit}).mappings()
    return {"count": total, "shown": limit, "students": [
        {"student_id": str(r["student_id"]), "prn": r["prn"], "name": r["name"], "school": r["school"],
         "allocated_at": iso_local(r["allocated_at"], offset_minutes), "stage_complete": bool(r["stage_complete"])} for r in rows]}


def exception_counters(conn: Connection) -> dict:
    by_type = {r["type"]: int(r["n"]) for r in conn.execute(
        text("SELECT type, count(*) AS n FROM exceptions WHERE status = 'OPEN' GROUP BY type ORDER BY type")).mappings()}
    one = lambda sql: int(conn.execute(text(sql)).scalar_one())  # noqa: E731
    return {
        "open": sum(by_type.values()), "open_by_type": by_type,
        "provisional_events": one("SELECT count(*) FROM activity_events WHERE 'PROVISIONAL' = ANY (flags)"),
        "manual_entries": one("SELECT count(*) FROM activity_events WHERE 'MANUAL' = ANY (flags)"),
        "duplicate_attempts": one("SELECT count(*) FROM scan_log WHERE result = 'DUPLICATE'"),
        "blocked_attempts": one("SELECT count(*) FROM scan_log WHERE result = 'REJECTED'"),
        "corrections": one("SELECT count(*) FROM activity_events WHERE kind IN ('REVERSAL','WAIVER')"),
    }


def _local_time(value, off):
    return iso_local(value, off) if value else None


def venue_health(conn: Connection, settings) -> dict:
    """Sync and freshness (SYSTEM_SPEC 9, 12). A venue shows how IT sees its link to central and how fresh each peer's
    data is here; central shows one traffic light per venue, from the heartbeat each venue sends with every push."""
    off = settings.event_utc_offset_minutes
    pending = conn.execute(text("SELECT count(*) AS n, min(created_at) AS oldest FROM outbox "
                                "WHERE sent_at IS NULL AND rejected_at IS NULL")).mappings().one()
    marks = conn.execute(text("SELECT max(last_success_at) AS pulled, max(last_push_at) AS pushed, "
                              "count(*) FILTER (WHERE last_error IS NOT NULL) AS failing FROM sync_state")).mappings().one()
    last_sync = max((t for t in (marks["pulled"], marks["pushed"]) if t), default=None)
    counts = {k: int(conn.execute(text(sql)).scalar_one()) for k, sql in {
        "parked": "SELECT count(*) FROM sync_parked",
        "rejected": "SELECT count(*) FROM outbox WHERE rejected_at IS NOT NULL",
        "conflicts": "SELECT count(*) FROM exceptions WHERE type = 'CONFLICT' AND status = 'OPEN'",
        "waiting_for_confirmation": "SELECT count(*) FROM exceptions WHERE type = 'PROVISIONAL_UNCONFIRMED' AND status = 'OPEN'",
        "gaps": "SELECT count(*) FROM exceptions WHERE type = 'SEQ_GAP' AND status = 'OPEN'"}.items()}
    out = {
        "server": "Central" if settings.mode == "central" else settings.venue_id, "mode": settings.mode, "database": "up",
        "pending_records": int(pending["n"]), "oldest_pending_at": _local_time(pending["oldest"], off),
        "last_sync_at": _local_time(last_sync, off), "sync_failures": int(marks["failing"]),
        "standby": "Not set up yet", "freshness_window_seconds": sync_status.freshness_window(conn), **counts,
        "peers": [], "venues": [], "last_error": None, "last_backup_at": None,
    }
    if settings.mode == "venue":
        me = sync_status.this_server(conn, settings)
        out.update(state=me["state"], emoji=me["emoji"], status=me["label"], last_error=me["last_error"])
        out["peers"] = [{**p, "data_as_of": _local_time(p["data_as_of"], off), "last_pull_at": _local_time(p["last_pull_at"], off)}
                        for p in sync_status.peers(conn)]
    else:
        venues = sync_status.venues_seen_by_central(conn)
        out["venues"] = [{**v, "last_sync_at": _local_time(v["last_sync_at"], off)} for v in venues]
        online = sum(1 for v in venues if v["state"] != sync_status.OFFLINE)
        overall = sync_status.ONLINE if online == len(venues) else sync_status.OFFLINE
        out.update(state=overall, emoji=sync_status.EMOJI[overall], status=f"{online} of {len(venues)} venues connected")
    if getattr(settings, "backup_dir", None):
        try:
            from backend.ha import backup
            latest = backup.latest_manifest(settings.backup_dir)
            out["last_backup_at"] = latest["created_at"] if latest else None
        except Exception:  # a missing or unreadable backup folder must never break the dashboard
            out["last_backup_at"] = None
    return out


def snapshot(conn: Connection, settings) -> dict:
    """Everything the dashboard shows, taken from one read-only transaction so the figures agree with each other."""
    conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    return {
        "as_of": iso_local(datetime.now(timezone.utc), settings.event_utc_offset_minutes),
        "counts": counts(conn), "school_wise": school_wise(conn), "funnel": funnel(conn), "stage": stage_view(conn),
        "outstanding_thobes": outstanding_thobes(conn, settings.event_utc_offset_minutes),
        "exceptions": exception_counters(conn), "health": venue_health(conn, settings),
        "activity_labels": ACTIVITY_LABEL,
    }

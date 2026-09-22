"""Reading and writing the one-row stage_state table, and the private (operator) view of it.

Every change goes through update_state() inside a transaction that first called begin_controller_txn();
a database trigger (migration 0006) refuses any other UPDATE, so nothing but the Stage Controller can move
the LED (golden rule 9).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

_FIELDS = {
    "current_student_id", "display_student_id", "previous_student_id",
    "controller_session_id", "controller_station_id", "controller_since", "controller_epoch",
}

CARD_SQL = """
    SELECT s.id, s.name, s.prn, s.programme, s.school, q.queue_position, q.status,
           EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) AS has_display_data
    FROM students s LEFT JOIN queue q ON q.student_id = s.id
"""


def begin_controller_txn(conn: Connection) -> None:
    """Mark THIS transaction as the Stage Controller's, so the guard trigger allows the update."""
    conn.execute(text("SELECT set_config('app.stage_controller', 'on', true)"))


def read_state(conn: Connection, *, lock: bool = False) -> dict:
    sql = "SELECT * FROM stage_state WHERE id = 1" + (" FOR UPDATE" if lock else "")
    return dict(conn.execute(text(sql)).mappings().one())


def update_state(conn: Connection, **fields) -> None:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ValueError(f"not a stage_state field: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(text(f"UPDATE stage_state SET {assignments} WHERE id = 1"), fields)


def has_display_data(conn: Connection, student_id) -> bool:
    return bool(conn.execute(text("SELECT EXISTS (SELECT 1 FROM display_snapshot WHERE student_id = :s)"), {"s": student_id}).scalar())


def private_card(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return {
        "student_id": str(row["id"]), "name": row["name"], "prn": row["prn"], "photo_url": f"/photo/{row['id']}",
        "programme": row["programme"], "school": row["school"],
        "queue_position": row["queue_position"], "status": row["status"], "has_display_data": bool(row["has_display_data"]),
    }


def card_for(conn: Connection, student_id) -> Optional[dict]:
    if student_id is None:
        return None
    row = conn.execute(text(CARD_SQL + " WHERE s.id = :i"), {"i": student_id}).mappings().one_or_none()
    return private_card(dict(row)) if row else None


def waiting(conn: Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        text(CARD_SQL + " WHERE q.status = 'QUEUED' ORDER BY q.queue_position LIMIT :n"), {"n": limit}
    ).mappings()
    return [private_card(dict(r)) for r in rows]


def controller_is_live(conn: Connection, state: dict, idle_minutes: int) -> bool:
    """True if the recorded controller's session is still a valid, signed-in session."""
    sid = state["controller_session_id"]
    if sid is None:
        return False
    return bool(conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM sessions s JOIN users u ON u.id = s.user_id AND u.active "
             "WHERE s.id = :sid AND s.revoked_at IS NULL AND s.expires_at > now() "
             "AND s.last_seen_at > now() - make_interval(mins => :idle))"),
        {"sid": sid, "idle": idle_minutes},
    ).scalar())


def private_state(conn: Connection, principal, settings) -> dict:
    """Everything the Stage screen shows: CURRENT / NEXT / AFTER NEXT, who controls, what the LED shows."""
    st = read_state(conn)
    ahead = waiting(conn, 2)
    controller = None
    if st["controller_session_id"] is not None:
        who = conn.execute(
            text("SELECT u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = :i"),
            {"i": st["controller_session_id"]},
        ).scalar()
        controller = {"username": who, "live": controller_is_live(conn, st, settings.session_idle_minutes)}
    led_name = None
    if st["display_student_id"] is not None:
        led_name = conn.execute(text("SELECT display_name FROM display_snapshot WHERE student_id = :s"),
                                {"s": st["display_student_id"]}).scalar()
    return {
        "version": st["version"],
        "you_control": st["controller_session_id"] is not None and str(st["controller_session_id"]) == principal.session_id,
        "controller": controller,
        "led_mode": "SHOWING" if st["display_student_id"] is not None else "HOME",
        "led_name": led_name,
        "current": card_for(conn, st["current_student_id"]),
        "next": ahead[0] if len(ahead) > 0 else None,
        "after_next": ahead[1] if len(ahead) > 1 else None,
        "previous": card_for(conn, st["previous_student_id"]),
        "queue_depth": conn.execute(text("SELECT count(*) FROM queue WHERE status = 'QUEUED'")).scalar_one(),
    }

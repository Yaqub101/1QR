"""The Stage Controller: DISPLAY NEXT, HOME, PREVIOUS, SEARCH, SKIP, COMPLETE, plus take-over.

Rules (SYSTEM_SPEC 13, 18; TODO Phase 11):
  * Exactly ONE laptop controls the stage. Every action takes the row lock on stage_state, then checks the
    caller's session is the recorded controller. "Take over" hands control to the caller and locks the old
    laptop out immediately (its next press is refused).
  * A rapid double press can never advance twice: the second press waits for the first's lock, then finds
    the state already changed (DISPLAY NEXT: "already on screen"; COMPLETE / SKIP: "nobody is on stage").
  * COMPLETE is recorded by the station engine (one code path for every activity) inside the SAME transaction
    as the state change, so the event and "who is on stage" can never disagree. SKIP needs a reason.
  * The LED shows only students who have an approved display_snapshot; without one it stays on the holding
    screen, and the operator is told.
  * A queue scan or confirmation never reaches this module, and the database refuses stage_state changes made
    anywhere else.
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.audit import write_audit
from backend.engine import service
from backend.engine.service import StationAccessError, TemporaryFailure
from backend.stage import state as stage_state

logger = logging.getLogger("backend.engine")  # same logger as the engine: technical detail lives in one log


class StageRefusal(Exception):
    """A plain-sentence refusal (HTTP 4xx). Nothing was changed."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


NOT_CONTROLLER = ("NOT_CONTROLLER", "This laptop is not running the stage. Use TAKE OVER.")


def _refuse(status: int, pair_or_code, message: Optional[str] = None):
    code, msg = pair_or_code if message is None else (pair_or_code, message)
    raise StageRefusal(status, code, msg)


def _run(engine, *, settings, principal, fn: Callable, needs_control: bool = True) -> dict:
    """One controller action: authorise, take the stage lock, check control, act, return the new private state."""
    try:
        with engine.begin() as conn:
            ctx = service.authorize_station(settings, principal, "STAGE")
            stage_state.begin_controller_txn(conn)
            st = stage_state.read_state(conn, lock=True)  # serialises every press, including a rapid double press
            if needs_control and (st["controller_session_id"] is None or str(st["controller_session_id"]) != principal.session_id):
                _refuse(409, NOT_CONTROLLER)
            outcome = fn(conn, ctx, st) or {}
            outcome.setdefault("changed", True)
            outcome["state"] = stage_state.private_state(conn, principal, settings)
        return {"ok": True, **outcome}
    except (StationAccessError, StageRefusal):
        raise
    except Exception as exc:
        logger.exception("stage action failed unexpectedly: operator=%s", principal.username)
        raise TemporaryFailure() from exc


def _audit(conn: Connection, ctx, action: str, student_id=None, **details) -> None:
    write_audit(conn, action, operator_id=ctx.principal.user_id, student_id=student_id, activity="STAGE", details=details)


# --------------------------------------------------------------------------- control
def claim(engine, **kw) -> dict:
    def act(conn, ctx, st):
        mine = st["controller_session_id"] is not None and str(st["controller_session_id"]) == ctx.principal.session_id
        if mine:
            return {"changed": False, "message": "This laptop is running the stage."}
        if stage_state.controller_is_live(conn, st, ctx.settings.session_idle_minutes):
            _refuse(409, "CONTROLLED_ELSEWHERE", "Another laptop is running the stage. Use TAKE OVER to replace it.")
        _set_controller(conn, ctx, st)
        return {"message": "This laptop is now running the stage."}
    return _run(engine, fn=act, needs_control=False, **kw)


def takeover(engine, **kw) -> dict:
    def act(conn, ctx, st):
        mine = st["controller_session_id"] is not None and str(st["controller_session_id"]) == ctx.principal.session_id
        if mine:
            return {"changed": False, "message": "This laptop is already running the stage."}
        _audit(conn, ctx, "STAGE_TAKEOVER",
               replaced_session=str(st["controller_session_id"]) if st["controller_session_id"] else None)
        _set_controller(conn, ctx, st)
        return {"message": "This laptop has taken over the stage."}
    return _run(engine, fn=act, needs_control=False, **kw)


def _set_controller(conn: Connection, ctx, st: dict) -> None:
    stage_state.update_state(
        conn, controller_session_id=ctx.principal.session_id,
        controller_since=conn.execute(text("SELECT now()")).scalar(), controller_epoch=st["controller_epoch"] + 1)


# --------------------------------------------------------------------------- the LED
def _show(conn: Connection, student_id) -> bool:
    """Point the LED at this student if (and only if) they have approved display data."""
    if stage_state.has_display_data(conn, student_id):
        stage_state.update_state(conn, display_student_id=student_id)
        return True
    stage_state.update_state(conn, display_student_id=None)
    return False


NO_DISPLAY_DATA = "This student has no approved display data, so the screen stays on the holding screen."


def display_next(engine, **kw) -> dict:
    def act(conn, ctx, st):
        if st["current_student_id"] is not None:  # somebody is already on stage: never skip ahead
            if st["display_student_id"] == st["current_student_id"]:
                return {"changed": False, "message": "Already on screen."}
            shown = _show(conn, st["current_student_id"])
            _audit(conn, ctx, "STAGE_DISPLAY", st["current_student_id"], via="resume")
            return {"message": "Showing again." if shown else NO_DISPLAY_DATA}
        row = conn.execute(
            text("SELECT student_id FROM queue WHERE status = 'QUEUED' ORDER BY queue_position LIMIT 1 FOR UPDATE")
        ).scalar()
        if row is None:
            _refuse(409, "QUEUE_EMPTY", "Nobody is waiting in the queue.")
        conn.execute(text("UPDATE queue SET status = 'DISPLAYED' WHERE student_id = :s"), {"s": row})
        stage_state.update_state(conn, current_student_id=row)
        shown = _show(conn, row)
        _audit(conn, ctx, "STAGE_DISPLAY", row, via="next")
        return {"message": "Showing the next student." if shown else NO_DISPLAY_DATA}
    return _run(engine, fn=act, **kw)


def home(engine, **kw) -> dict:
    def act(conn, ctx, st):
        if st["display_student_id"] is None:
            return {"changed": False, "message": "Already on the holding screen."}
        stage_state.update_state(conn, display_student_id=None)
        _audit(conn, ctx, "STAGE_HOME", st["display_student_id"])
        return {"message": "Holding screen."}
    return _run(engine, fn=act, **kw)


def previous(engine, **kw) -> dict:
    """With someone on stage: put them back at the front of the queue (undo a wrong DISPLAY NEXT).
    With nobody on stage: show the last student to leave the stage again (display only)."""
    def act(conn, ctx, st):
        if st["current_student_id"] is not None:
            conn.execute(text("UPDATE queue SET status = 'QUEUED' WHERE student_id = :s"), {"s": st["current_student_id"]})
            stage_state.update_state(conn, current_student_id=None, display_student_id=None)
            _audit(conn, ctx, "STAGE_PREVIOUS", st["current_student_id"], returned_to_queue=True)
            return {"message": "Student returned to the front of the queue."}
        if st["previous_student_id"] is None:
            _refuse(409, "NO_PREVIOUS", "There is no previous student.")
        shown = _show(conn, st["previous_student_id"])
        _audit(conn, ctx, "STAGE_PREVIOUS", st["previous_student_id"], replayed=True)
        return {"message": "Showing the previous student again." if shown else NO_DISPLAY_DATA}
    return _run(engine, fn=act, **kw)


def search(engine, *, query: str, **kw) -> dict:
    def act(conn, ctx, st):
        needle = " ".join((query or "").split())
        rows = []
        if needle:
            rows = conn.execute(
                text(stage_state.CARD_SQL + " WHERE q.status IN ('QUEUED','HELD','SKIPPED') "
                     "AND (s.prn ILIKE :like OR s.name ILIKE :like) ORDER BY q.queue_position LIMIT 10"),
                {"like": f"%{needle}%"},
            ).mappings().all()
        _audit(conn, ctx, "STAGE_SEARCH", query=needle, found=len(rows))
        return {"changed": False, "matches": [stage_state.private_card(dict(r)) for r in rows],
                "message": f"{len(rows)} found." if rows else "Nobody in the queue matches that."}
    return _run(engine, fn=act, **kw)


def display(engine, *, student_id: str, **kw) -> dict:
    """Show a specific waiting student (a SEARCH result), out of order if need be."""
    def act(conn, ctx, st):
        if st["current_student_id"] is not None:
            _refuse(409, "STAGE_BUSY", "Complete or skip the student on stage first.")
        row = None
        try:
            wanted = uuid.UUID(str(student_id))
        except ValueError:
            wanted = None  # a malformed id must never reach SQL: a bad cast would abort the whole transaction
        if wanted is not None:
            row = conn.execute(
                text("SELECT student_id, queue_position, status FROM queue WHERE student_id = :s "
                     "AND status IN ('QUEUED','HELD','SKIPPED') FOR UPDATE"), {"s": wanted}).mappings().one_or_none()
        if row is None:
            _refuse(409, "NOT_WAITING", "That student is not waiting in the queue.")
        head = conn.execute(text("SELECT min(queue_position) FROM queue WHERE status = 'QUEUED'")).scalar()
        conn.execute(text("UPDATE queue SET status = 'DISPLAYED' WHERE student_id = :s"), {"s": row["student_id"]})
        stage_state.update_state(conn, current_student_id=row["student_id"])
        shown = _show(conn, row["student_id"])
        _audit(conn, ctx, "STAGE_DISPLAY", row["student_id"], via="search",
               out_of_order=(head is not None and row["queue_position"] != head))
        return {"message": "Showing the selected student." if shown else NO_DISPLAY_DATA}
    return _run(engine, fn=act, **kw)


# --------------------------------------------------------------------------- COMPLETE / SKIP
def _leave_stage(conn: Connection, student_id, queue_status: str) -> None:
    conn.execute(text("UPDATE queue SET status = :st WHERE student_id = :s"), {"st": queue_status, "s": student_id})
    stage_state.update_state(conn, current_student_id=None, display_student_id=None, previous_student_id=student_id)


def complete(engine, *, settings, principal) -> dict:
    def act(conn, ctx, st):
        student_id = st["current_student_id"]
        if student_id is None:
            _refuse(409, "NOTHING_ON_STAGE", "Nobody is on stage.")
        # The engine records the Stage event (event + audit + scan_log) in THIS transaction. The
        # student came from the queue, not from a typed PRN, so it is not flagged MANUAL.
        result = service.confirm_in_transaction(
            conn, settings=settings, principal=principal, activity="STAGE",
            student_id=str(student_id), manual=False)
        if result.result not in ("CONFIRMED", "DUPLICATE"):  # DUPLICATE: already recorded; just finish the hand-over
            _refuse(409, "CANNOT_COMPLETE", result.message)
        _leave_stage(conn, student_id, "DONE")
        return {"message": "Degree recorded."}
    return _run(engine, settings=settings, principal=principal, fn=act)


def skip(engine, *, reason: Optional[str], settings, principal) -> dict:
    def act(conn, ctx, st):
        clean = (reason or "").strip()
        if not clean:
            _refuse(400, "REASON_REQUIRED", "Please give a reason for skipping.")
        student_id = st["current_student_id"]
        if student_id is None:
            _refuse(409, "NOTHING_ON_STAGE", "Nobody is on stage.")
        student = {"id": student_id}
        service.record_skip(conn, ctx, student, clean)
        _leave_stage(conn, student_id, "SKIPPED")
        return {"message": "Skipped."}
    return _run(engine, settings=settings, principal=principal, fn=act)

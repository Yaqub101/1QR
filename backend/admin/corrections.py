"""Admin corrections (SYSTEM_SPEC 16): reverse a completed activity, or waive a Thobe Return.

THE RULE THIS MODULE EXISTS TO KEEP (golden rule 5): a correction is a NEW row that points at the original.
The original activity_events row is never updated, never deleted, never "flagged". How that is guaranteed:

  1. This module touches activity_events with exactly two statements: a SELECT (to read the original) and one
     INSERT (the new REVERSAL or WAIVER row). There is no UPDATE or DELETE of activity_events anywhere in
     backend/ (a test greps the source for it).
  2. The original's "reversed" state is NOT stored on it. It is derived: a completion is active unless a
     REVERSAL row exists for its (student, activity, cycle). Nothing has to be written to the original for the
     status to change (migration 0004's view and backend/engine/queries.py use the same definition).
  3. Even a bug here could not change history: PostgreSQL triggers (migration 0003) refuse UPDATE, DELETE and
     TRUNCATE of that table for every role, including the table owner the app connects as.
  4. The reversal, its outbox row, its audit row and any exception row commit in ONE transaction, or none do.

Only the owning venue applies a correction (golden rule 4). Corrections for another venue's activity are not
written here; the caller is told plainly where to make them (queueing them through sync is Phase 14/15).
Admin only, enforced here as well as on the route, so no future caller can reach these without the role.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit
from backend.engine import service
from backend.engine.queries import active_completion, next_completion_cycle
from backend.security import ownership, permissions

logger = logging.getLogger("backend.admin")

MAX_REASON_LENGTH = 500
NOT_HERE = "{label} is recorded at the {venue} server, so this correction has to be made there."


class CorrectionError(Exception):
    """A refusal with an HTTP status. Nothing was written. `message` is one plain sentence."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def clean_reason(reason) -> str:
    """The reason is mandatory and is checked HERE, on the server: blank, whitespace-only or missing is refused."""
    value = " ".join(str(reason or "").split())
    if not value:
        raise CorrectionError(400, "REASON_REQUIRED", "Please give a reason for this correction.")
    if len(value) > MAX_REASON_LENGTH:
        raise CorrectionError(400, "REASON_TOO_LONG", f"Please keep the reason under {MAX_REASON_LENGTH} characters.")
    return value


def _require_admin(principal) -> None:
    if not permissions.has_permission(principal.role, permissions.ADMIN_PERMISSION):
        raise CorrectionError(403, "FORBIDDEN", "Only the Admin can make corrections.")


def _apply_here(guard, activity: str) -> None:
    route = guard.route_correction(activity)
    if not route.apply_here:
        raise CorrectionError(
            409, "APPLY_AT_OWNER",
            NOT_HERE.format(label=ownership.ACTIVITY_LABEL[activity], venue=ownership.VENUE_LABEL[route.owner_venue]))


# ------------------------------------------------------------------ the write steps (replaceable in tests)
def insert_correction_event(conn: Connection, *, student_id, activity: str, kind: str, venue_id: str, operator_id,
                            cycle: int, details: dict, corrects_event_id=None) -> dict:
    """THE ONLY WRITE to activity_events in this module: one INSERT of a new row. No station (an Admin acted),
    always flagged CORRECTED, and for a REVERSAL the original's id goes in corrects_event_id."""
    row = conn.execute(
        text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details, "
             "completion_cycle, corrects_event_id) VALUES (:s, :a, :k, :v, NULL, :o, ARRAY['CORRECTED']::text[], "
             "CAST(:d AS jsonb), :c, :x) RETURNING event_id, venue_seq, server_time"),
        {"s": student_id, "a": activity, "k": kind, "v": venue_id, "o": operator_id, "d": json.dumps(details, default=str),
         "c": cycle, "x": corrects_event_id},
    ).mappings().one()
    return dict(row)


def insert_outbox(conn: Connection, event_id) -> None:
    service.insert_outbox(conn, event_id)


def insert_audit(conn: Connection, *, action: str, principal, student_id, activity: str, venue_id: str, event: dict,
                 reason: str, details: dict, corrects_event_id) -> None:
    write_audit(conn, action, operator_id=principal.user_id, venue_id=venue_id, reason=reason, details=details,
                student_id=student_id, activity=activity, event_id=event["event_id"], venue_seq=event["venue_seq"],
                flags=["CORRECTED"], corrects_event_id=corrects_event_id, corrected_by=principal.user_id)


# ------------------------------------------------------------------ queue side effects of a reversal
def _queue_effects(conn: Connection, student_id, activity: str) -> Optional[str]:
    """Keep the Stage queue truthful. Only the `queue` table is touched: never the LED (stage_state), which
    only the Stage Controller can move (golden rule 9, and a trigger enforces it).
      QUEUE reversed : the student is no longer queued, so they must not be shown on stage -> drop the queue row.
      STAGE reversed : the student never received the degree -> back to QUEUED at their old position.
    A student who is on stage RIGHT NOW cannot have their Queue reversed (it would strand the Stage screen)."""
    if activity == "QUEUE":
        status = conn.execute(text("SELECT status FROM queue WHERE student_id = :s FOR UPDATE"), {"s": student_id}).scalar()
        if status == "DISPLAYED":
            raise CorrectionError(409, "ON_STAGE_NOW", "That student is on stage right now. Send them back on the Stage screen first.")
        if status is not None:
            conn.execute(text("DELETE FROM queue WHERE student_id = :s"), {"s": student_id})
            return "queue row removed"
    elif activity == "STAGE":
        moved = conn.execute(
            text("UPDATE queue SET status = 'QUEUED' WHERE student_id = :s AND status = 'DONE' RETURNING 1"), {"s": student_id}
        ).scalar()
        if moved:
            return "returned to the queue"
    return None


# ------------------------------------------------------------------ reverse
def reverse_event(engine, *, guard, principal, event_id, reason) -> dict:
    """Reverse a completed activity (or a waiver). Writes ONE new REVERSAL event that references `event_id`."""
    _require_admin(principal)
    reason = clean_reason(reason)
    try:
        with engine.begin() as conn:
            result = _reverse(conn, guard=guard, principal=principal, event_id=event_id, reason=reason)
        return result  # only after COMMIT (golden rule 6)
    except CorrectionError:
        raise
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in ("activity_events_one_reversal", "activity_events_one_completion"):
            raise CorrectionError(409, "ALREADY_REVERSED", "That record has already been reversed.") from exc
        logger.exception("reversal failed on a database rule: event_id=%s", event_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc
    except Exception as exc:
        logger.exception("reversal failed unexpectedly: event_id=%s", event_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc


def _reverse(conn: Connection, *, guard, principal, event_id, reason: str) -> dict:
    original = None
    if _is_uuid(event_id):  # a malformed id must never reach SQL: a bad cast would abort the transaction
        original = conn.execute(text("SELECT * FROM activity_events WHERE event_id = CAST(:e AS uuid)"),
                                {"e": str(event_id)}).mappings().one_or_none()
    if original is None:
        raise CorrectionError(404, "EVENT_NOT_FOUND", "That record does not exist.")
    if original["kind"] not in ("COMPLETE", "WAIVER"):
        raise CorrectionError(409, "NOT_REVERSIBLE", "Only a completed activity or a waiver can be reversed.")
    activity = original["activity"]
    _apply_here(guard, activity)

    already = conn.execute(
        text("SELECT 1 FROM activity_events WHERE kind = 'REVERSAL' AND student_id = :s AND activity = :a AND completion_cycle = :c"),
        {"s": original["student_id"], "a": activity, "c": original["completion_cycle"]},
    ).scalar()
    if already:
        raise CorrectionError(409, "ALREADY_REVERSED", "That record has already been reversed.")

    later = [r["activity"] for r in conn.execute(
        text("SELECT DISTINCT e.activity FROM activity_events e WHERE e.student_id = :s AND e.kind IN ('COMPLETE','WAIVER') "
             "AND activity_step(e.activity) > activity_step(:a) AND NOT EXISTS (SELECT 1 FROM activity_events r "
             "WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id AND r.activity = e.activity "
             "AND r.completion_cycle = e.completion_cycle)"),
        {"s": original["student_id"], "a": activity}).mappings()]
    details = {
        "reason": reason, "corrects_kind": original["kind"], "original_server_time": original["server_time"].isoformat(),
        "original_venue_seq": original["venue_seq"], "later_activities_still_recorded": sorted(later),
    }
    side_effect = _queue_effects(conn, original["student_id"], activity)
    if side_effect:
        details["queue"] = side_effect

    event = insert_correction_event(
        conn, student_id=original["student_id"], activity=activity, kind="REVERSAL", venue_id=original["venue_id"],
        operator_id=principal.user_id, cycle=original["completion_cycle"], details=details,
        corrects_event_id=original["event_id"])
    insert_outbox(conn, event["event_id"])
    insert_audit(conn, action="ADMIN_REVERSAL", principal=principal, student_id=original["student_id"], activity=activity,
                 venue_id=original["venue_id"], event=event, reason=reason, details=details, corrects_event_id=original["event_id"])
    return {"ok": True, "message": "Reversed. The original record is kept.", "correction_event_id": str(event["event_id"]),
            "corrects_event_id": str(original["event_id"]), "activity": activity, "kind": "REVERSAL",
            "later_activities_still_recorded": sorted(later)}


# ------------------------------------------------------------------ waive a lost / unreturned thobe
def waive_return(engine, *, guard, principal, student_id, reason) -> dict:
    """Admin "Return Waived / Lost": counts as the Thobe Return for Lunch, flagged CORRECTED, reason mandatory."""
    _require_admin(principal)
    reason = clean_reason(reason)
    try:
        with engine.begin() as conn:
            result = _waive(conn, guard=guard, principal=principal, student_id=student_id, reason=reason)
        return result
    except CorrectionError:
        raise
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "activity_events_one_completion":
            raise CorrectionError(409, "ALREADY_RETURNED", "That thobe is already recorded as returned or waived.") from exc
        logger.exception("waiver failed on a database rule: student_id=%s", student_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc
    except Exception as exc:
        logger.exception("waiver failed unexpectedly: student_id=%s", student_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc


def _waive(conn: Connection, *, guard, principal, student_id, reason: str) -> dict:
    activity = "THOBE_RETURN"
    _apply_here(guard, activity)
    student = None
    if _is_uuid(student_id):
        student = conn.execute(text("SELECT id, prn, name FROM students WHERE id = CAST(:s AS uuid)"),
                               {"s": str(student_id)}).mappings().one_or_none()
    if student is None:
        raise CorrectionError(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    if active_completion(conn, student["id"], activity) is not None:
        raise CorrectionError(409, "ALREADY_RETURNED", "That thobe is already recorded as returned or waived.")
    allocation = active_completion(conn, student["id"], "THOBE_ALLOCATION")
    details = {"reason": reason, "thobe_allocation_event_id": str(allocation["event_id"]) if allocation else None,
               "thobe_allocation_on_record": allocation is not None}
    cycle = next_completion_cycle(conn, student["id"], activity)
    event = insert_correction_event(conn, student_id=student["id"], activity=activity, kind="WAIVER", venue_id="hall",
                                    operator_id=principal.user_id, cycle=cycle, details=details)
    insert_outbox(conn, event["event_id"])
    # A WAIVER row cannot carry corrects_event_id (that column is reserved for reversals by a CHECK), so the
    # audit row links it to the thobe allocation it writes off.
    insert_audit(conn, action="RETURN_WAIVED", principal=principal, student_id=student["id"], activity=activity, venue_id="hall",
                 event=event, reason=reason, details=details, corrects_event_id=allocation["event_id"] if allocation else None)
    conn.execute(
        text("INSERT INTO exceptions (type, student_id, venue_id, event_id, details) VALUES ('RETURN_WAIVED', :s, 'hall', :e, "
             "CAST(:d AS jsonb))"),
        {"s": student["id"], "e": event["event_id"],
         "d": json.dumps({"reason": reason, "waived_by": str(principal.user_id), "prn": student["prn"]}, default=str)},
    )
    return {"ok": True, "message": "Return waived. The student can now go to Lunch.", "correction_event_id": str(event["event_id"]),
            "activity": activity, "kind": "WAIVER", "thobe_allocation_on_record": allocation is not None}


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False

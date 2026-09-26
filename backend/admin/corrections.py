"""Admin corrections (SYSTEM_SPEC 16): reverse a completed activity, or waive a Robe Return.

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
  4. The reversal and its audit row commit in ONE transaction, or neither does.

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
from backend.engine.queries import active_completion, next_completion_cycle
from backend.security import permissions

logger = logging.getLogger("backend.admin")

MAX_REASON_LENGTH = 500


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


# ------------------------------------------------------------------ the write steps (replaceable in tests)
def insert_correction_event(conn: Connection, *, student_id, activity: str, kind: str, operator_id,
                            cycle: int, details: dict, corrects_event_id=None) -> dict:
    """THE ONLY WRITE to activity_events in this module: one INSERT of a new row. Always flagged CORRECTED,
    and for a REVERSAL the original's id goes in corrects_event_id."""
    row = conn.execute(
        text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, "
             "completion_cycle, corrects_event_id) VALUES (:s, :a, :k, :o, ARRAY['CORRECTED']::text[], "
             "CAST(:d AS jsonb), :c, :x) RETURNING event_id, server_time"),
        {"s": student_id, "a": activity, "k": kind, "o": operator_id, "d": json.dumps(details, default=str),
         "c": cycle, "x": corrects_event_id},
    ).mappings().one()
    return dict(row)


def insert_audit(conn: Connection, *, action: str, principal, student_id, activity: str, event: dict,
                 reason: str, details: dict, corrects_event_id) -> None:
    write_audit(conn, action, operator_id=principal.user_id, reason=reason, details=details,
                student_id=student_id, activity=activity, event_id=event["event_id"],
                flags=["CORRECTED"], corrects_event_id=corrects_event_id, corrected_by=principal.user_id)


# ------------------------------------------------------------------ queue side effects of a reversal
def _queue_effects(conn: Connection, student_id, activity: str) -> Optional[str]:
    """Keep the Queue truthful. Only the `queue` table is touched.
      QUEUE reversed : the student is no longer queued."""
    if activity == "QUEUE":
        status = conn.execute(text("SELECT status FROM queue WHERE student_id = :s FOR UPDATE"), {"s": student_id}).scalar()
        if status is not None:
            conn.execute(text("DELETE FROM queue WHERE student_id = :s"), {"s": student_id})
            return "queue row removed"
    return None


# ------------------------------------------------------------------ reverse
def reverse_event(engine, *, principal, event_id, reason) -> dict:
    """Reverse a completed activity (or a waiver). Writes ONE new REVERSAL event that references `event_id`."""
    _require_admin(principal)
    reason = clean_reason(reason)
    try:
        with engine.begin() as conn:
            result = _reverse(conn, principal=principal, event_id=event_id, reason=reason)
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


def _reverse(conn: Connection, *, principal, event_id, reason: str) -> dict:
    original = None
    if _is_uuid(event_id):  # a malformed id must never reach SQL: a bad cast would abort the transaction
        original = conn.execute(text("SELECT * FROM activity_events WHERE event_id = CAST(:e AS uuid)"),
                                {"e": str(event_id)}).mappings().one_or_none()
    if original is None:
        raise CorrectionError(404, "EVENT_NOT_FOUND", "That record does not exist.")
    if original["kind"] not in ("COMPLETE", "WAIVER"):
        raise CorrectionError(409, "NOT_REVERSIBLE", "Only a completed activity or a waiver can be reversed.")
    activity = original["activity"]

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
        "later_activities_still_recorded": sorted(later),
    }
    side_effect = _queue_effects(conn, original["student_id"], activity)
    if side_effect:
        details["queue"] = side_effect

    event = insert_correction_event(
        conn, student_id=original["student_id"], activity=activity, kind="REVERSAL",
        operator_id=principal.user_id, cycle=original["completion_cycle"], details=details,
        corrects_event_id=original["event_id"])
    insert_audit(conn, action="ADMIN_REVERSAL", principal=principal, student_id=original["student_id"], activity=activity,
                 event=event, reason=reason, details=details, corrects_event_id=original["event_id"])
    return {"ok": True, "message": "Reversed. The original record is kept.", "correction_event_id": str(event["event_id"]),
            "corrects_event_id": str(original["event_id"]), "activity": activity, "kind": "REVERSAL",
            "later_activities_still_recorded": sorted(later)}


# ------------------------------------------------------------------ waive a return: lost robe / money kept
# The two returns an Admin may settle without the item coming back. Each counts as done for Lunch, is flagged
# CORRECTED, needs a reason, and opens an exception for the Admin's review list.
_WAIVERS = {
    "THOBE_RETURN": {"given": "THOBE_ALLOCATION", "action": "RETURN_WAIVED",
                     "already": ("ALREADY_RETURNED", "That robe is already recorded as returned or waived."),
                     "done": "Return waived."},
}


def waive_return(engine, *, principal, student_id, reason) -> dict:
    """Admin "Return Waived / Lost": counts as the Robe Return for Lunch, flagged CORRECTED, reason mandatory."""
    return _waive_in_transaction(engine, "THOBE_RETURN", principal=principal, student_id=student_id, reason=reason)


def _waive_in_transaction(engine, activity: str, *, principal, student_id, reason) -> dict:
    _require_admin(principal)
    reason = clean_reason(reason)
    rule = _WAIVERS[activity]
    try:
        with engine.begin() as conn:
            result = _waive(conn, activity, principal=principal, student_id=student_id, reason=reason)
        return result
    except CorrectionError:
        raise
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "activity_events_one_completion":
            raise CorrectionError(409, *rule["already"]) from exc
        logger.exception("waiver failed on a database rule: student_id=%s", student_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc
    except Exception as exc:
        logger.exception("waiver failed unexpectedly: student_id=%s", student_id)
        raise CorrectionError(503, "TEMPORARY", "One moment, please try again.") from exc


def _waive(conn: Connection, activity: str, *, principal, student_id, reason: str) -> dict:
    rule = _WAIVERS[activity]
    student = None
    if _is_uuid(student_id):
        student = conn.execute(text("SELECT id, prn, name FROM students WHERE id = CAST(:s AS uuid)"),
                               {"s": str(student_id)}).mappings().one_or_none()
    if student is None:
        raise CorrectionError(404, "STUDENT_NOT_FOUND", "That student does not exist.")
    if active_completion(conn, student["id"], activity) is not None:
        raise CorrectionError(409, *rule["already"])
    given = active_completion(conn, student["id"], rule["given"])
    on_record = rule["given"].lower() + "_on_record"
    details = {"reason": reason, rule["given"].lower() + "_event_id": str(given["event_id"]) if given else None,
               on_record: given is not None}
    cycle = next_completion_cycle(conn, student["id"], activity)
    event = insert_correction_event(conn, student_id=student["id"], activity=activity, kind="WAIVER",
                                    operator_id=principal.user_id, cycle=cycle, details=details)
    # A WAIVER row cannot carry corrects_event_id (that column is reserved for reversals by a CHECK), so the
    # audit row links it to the robe allocation / money receipt it writes off.
    insert_audit(conn, action=rule["action"], principal=principal, student_id=student["id"], activity=activity,
                 event=event, reason=reason, details=details, corrects_event_id=given["event_id"] if given else None)
    conn.execute(
        text("INSERT INTO exceptions (type, student_id, event_id, details) VALUES (:t, :s, :e, CAST(:d AS jsonb))"),
        {"t": rule["action"], "s": student["id"], "e": event["event_id"],
         "d": json.dumps({"reason": reason, "waived_by": str(principal.user_id), "prn": student["prn"]}, default=str)},
    )
    message = rule["done"] + " The student can now go to Lunch."
    return {"ok": True, "message": message, "correction_event_id": str(event["event_id"]),
            "activity": activity, "kind": "WAIVER", on_record: given is not None}


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False

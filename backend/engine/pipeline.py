"""The scan pipeline (SYSTEM_SPEC section 6), the same for every activity:

    identify student (QR token or PRN)  ->  student ACTIVE?  ->  prerequisites met?
        ->  already completed at this activity?  ->  READY (show the card)

Nothing here is per-activity: what differs between activities is the ActivityConfig in
activities.py. Nothing here writes; it only decides.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.engine import cross_venue, messages
from backend.engine.context import EngineContext
from backend.engine.queries import active_completion, clock_text
from backend.security import ownership

logger = logging.getLogger("backend.engine")

MAX_TOKEN_LENGTH = 256
MAX_PRN_LENGTH = 64
_STUDENT_COLUMNS = "s.id, s.prn, s.name, s.programme, s.school, s.sequence_no, s.seat_no, s.status"


@dataclass
class Outcome:
    result: str                    # READY | DUPLICATE | REJECTED | INVALID
    message: str                   # what the operator reads: one plain sentence
    log_result: Optional[str] = None  # scan_log result for a terminal outcome; None while READY
    student: Optional[dict] = None
    earlier: Optional[dict] = None
    provisional: bool = False
    rule: Optional[str] = None     # technical: which rule fired (log only, never on screen)
    detail: Optional[str] = None   # technical: extra detail (log only)


# ------------------------------------------------------------------ identify
def normalise_token(raw) -> str:
    """Strip the scanner's trailing Enter/newline/tab/NUL (and anything after the first line)."""
    if not isinstance(raw, str):
        return ""
    for line in re.split(r"[\r\n]+", raw.replace("\x00", "")):
        if line.strip():
            return line.strip()
    return ""


def normalise_prn(raw) -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def identify_by_token(conn: Connection, token: str):
    """-> (student | None, Outcome | None). The Outcome is set when the QR cannot be used."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None, Outcome("INVALID", messages.UNKNOWN_QR, "INVALID", rule="invalid_token", detail="blank or oversized token")
    row = conn.execute(
        text(f"SELECT t.active AS token_active, {_STUDENT_COLUMNS} FROM qr_tokens t "
             "JOIN students s ON s.id = t.student_id WHERE t.token = :t"),
        {"t": token},
    ).mappings().one_or_none()
    if row is None:
        return None, Outcome("INVALID", messages.UNKNOWN_QR, "INVALID", rule="invalid_token", detail="no such token")
    student = {k: v for k, v in row.items() if k != "token_active"}
    if not row["token_active"]:
        return student, Outcome("INVALID", messages.REPLACED_QR, "INVALID", student=student, rule="invalid_token",
                                detail="token was deactivated (reissued)")
    return student, None


def identify_by_prn(conn: Connection, prn: str):
    if not prn or len(prn) > MAX_PRN_LENGTH:
        return None, Outcome("INVALID", messages.STUDENT_NOT_FOUND, "INVALID", rule="prn_not_found", detail="blank or oversized PRN")
    row = conn.execute(
        text(f"SELECT {_STUDENT_COLUMNS} FROM students s WHERE upper(s.prn) = upper(:p)"), {"p": prn}
    ).mappings().one_or_none()
    if row is None:
        return None, Outcome("INVALID", messages.STUDENT_NOT_FOUND, "INVALID", rule="prn_not_found", detail="no such PRN")
    return dict(row), None


def identify_by_id(conn: Connection, student_id: str):
    try:
        sid = uuid.UUID(str(student_id))
    except ValueError:
        return None, Outcome("INVALID", messages.STUDENT_NOT_FOUND, "INVALID", rule="student_not_found", detail="not a valid id")
    row = conn.execute(
        text(f"SELECT {_STUDENT_COLUMNS} FROM students s WHERE s.id = :i"), {"i": sid}
    ).mappings().one_or_none()
    if row is None:
        return None, Outcome("INVALID", messages.STUDENT_NOT_FOUND, "INVALID", rule="student_not_found", detail="no such student")
    return dict(row), None


# ------------------------------------------------------------------ decide
class _Defaults(dict):
    def __missing__(self, key):
        return "n/a"


def duplicate_text(ctx: EngineContext, earlier: dict) -> str:
    details = earlier.get("details") or {}
    values = _Defaults(
        time=clock_text(earlier["server_time"], ctx.settings.event_utc_offset_minutes),
        station=earlier.get("station_id") or "the Admin console",
        seat_no=details.get("seat_no") or "not assigned",
        queue_position=details.get("queue_position") if details.get("queue_position") is not None else "n/a",
    )
    return ctx.config.duplicate_message.format_map(values)


def evaluate(conn: Connection, ctx: EngineContext, student: dict) -> Outcome:
    """Decide what may happen for this student at this station right now."""
    cfg = ctx.config
    sid = student["id"]

    if student["status"] != "ACTIVE":
        return Outcome("REJECTED", messages.STUDENT_INACTIVE, "REJECTED", student=student, rule="student_inactive",
                       detail=f"student status is {student['status']}")

    provisional = False
    for prerequisite in cfg.prerequisites:
        present = active_completion(conn, sid, prerequisite.activity) is not None
        if ownership.ACTIVITY_OWNER[prerequisite.activity] == ctx.venue:
            # SAME venue: the data is local and current, so this is a hard block (golden rule 8).
            if not present:
                return Outcome("REJECTED", prerequisite.missing_message, "REJECTED", student=student,
                               rule=f"prerequisite:{prerequisite.activity}", detail="same-venue prerequisite missing")
            continue
        # DIFFERENT venue: the data may be stale, so this goes through the one Phase 15 hook.
        decision = cross_venue.check_cross_venue_prerequisite(
            conn, student_id=sid, prerequisite=prerequisite, this_venue=ctx.venue, present_locally=present)
        if not decision.allow:
            return Outcome("REJECTED", decision.message or prerequisite.missing_message, "REJECTED", student=student,
                           rule=f"cross_venue_prerequisite:{prerequisite.activity}", detail="blocked by the cross-venue rule")
        provisional = provisional or decision.provisional

    earlier = active_completion(conn, sid, ctx.activity)
    if earlier is not None:
        return Outcome(
            "DUPLICATE", duplicate_text(ctx, earlier), "DUPLICATE", student=student, rule="already_completed",
            earlier={"time": clock_text(earlier["server_time"], ctx.settings.event_utc_offset_minutes),
                     "station_id": earlier["station_id"], "event_id": str(earlier["event_id"]), "kind": earlier["kind"]},
            detail=f"earlier event {earlier['event_id']}",
        )
    return Outcome("READY", messages.READY, None, student=student, provisional=provisional)

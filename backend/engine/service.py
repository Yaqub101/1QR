"""The station engine: scan / search (preview) and confirm (the only write).

    scan / search   read only. Identify the student, run the pipeline, return the card.
    confirm         ONE database transaction writes, together and only together:
                        effects (e.g. the queue row) -> the activity event -> its outbox row
                        -> the audit row -> the scan_log row
                    and the caller sees success only after that transaction has COMMITTED
                    (golden rule 6). It re-runs every check inside the transaction, so a client
                    can never skip the preview, and the Phase 2 unique index is the final judge of
                    a race between two stations.

The activity is ALWAYS the bound station's (golden rule 2); the request never names one.
Operator text is plain; technical detail goes to the `backend.engine` logger only (rule 11).

The small write steps below are module-level functions on purpose: tests replace them to simulate a
crash between two writes and prove nothing half-written survives.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit
from backend.engine import extensions, messages, pipeline
from backend.engine.activities import ACTIVITY_CONFIGS
from backend.engine.context import EngineContext
from backend.engine.pipeline import Outcome
from backend.engine.queries import clock_text, next_completion_cycle
from backend.security import ownership, permissions

logger = logging.getLogger("backend.engine")

# The unique constraints that mean "someone completed this a moment ago": the Phase 2 index on
# activity_events, and the queue table's primary key (a student is queued once).
DUPLICATE_CONSTRAINTS = {"activity_events_one_completion", "queue_pkey"}


class StationAccessError(Exception):
    """The caller may not use this station. Maps to an HTTP 4xx; the message is operator-safe."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


class TemporaryFailure(Exception):
    """Something unexpected went wrong (already logged in full). The operator sees a calm retry message."""

    message = messages.TEMPORARY


@dataclass
class EngineResult:
    result: str
    message: str
    activity: str
    station_id: str
    manual: bool = False
    student: Optional[dict] = None
    earlier: Optional[dict] = None
    event: Optional[dict] = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "result": self.result, "colour": messages.COLOUR[self.result], "message": self.message,
            "activity": self.activity, "station_id": self.station_id, "manual": self.manual,
            "student": self.student, "earlier": self.earlier, "event": self.event,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


# ------------------------------------------------------------------ access
def _context(conn: Connection, settings, guard, principal, station_id: str) -> EngineContext:
    station = conn.execute(
        text("SELECT station_id, venue_id, activity, active FROM stations WHERE station_id = :s"), {"s": station_id}
    ).mappings().one_or_none()
    if station is None:
        raise StationAccessError(404, "STATION_NOT_FOUND", "That station does not exist.")
    if not station["active"]:
        raise StationAccessError(403, "STATION_INACTIVE", "That station is switched off.")
    try:
        guard.ensure_can_originate(station["activity"])  # golden rule 4: only the owning venue records this
    except ownership.OwnershipError as exc:
        raise StationAccessError(403, exc.code, exc.message) from exc
    if not permissions.can_use_activity(principal.role, station["activity"]):
        raise StationAccessError(403, "FORBIDDEN", "That screen is not part of your role.")
    if not principal.is_admin and principal.station_id != station["station_id"]:
        raise StationAccessError(403, "STATION_MISMATCH", "This laptop is set up for another station.")
    return EngineContext(settings=settings, principal=principal, station=dict(station),
                         config=ACTIVITY_CONFIGS[station["activity"]])


# ------------------------------------------------------------------ helpers
def build_card(conn: Connection, ctx: EngineContext, student: dict) -> dict:
    fields = []
    for key in ctx.config.display_fields:
        spec = extensions.DISPLAY_FIELDS[key]
        fields.append({"key": key, "label": spec.label, "value": spec.provider(conn, student, ctx)})
    return {"student_id": str(student["id"]), "name": student["name"], "photo_url": f"/photo/{student['id']}", "fields": fields}


def log_attempt(conn: Connection, ctx: EngineContext, *, result: str, message: str, student_id=None, token=None,
                prn=None, event_id=None, details: Optional[dict] = None) -> None:
    conn.execute(
        text("INSERT INTO scan_log (venue_id, station_id, activity, operator_id, student_id, token_presented, "
             "prn_entered, result, message, event_id, details) VALUES (:v, :s, :a, :o, :st, :tok, :prn, :r, :m, :e, "
             "CAST(:d AS jsonb))"),
        {"v": ctx.venue, "s": ctx.station["station_id"], "a": ctx.activity, "o": ctx.principal.user_id, "st": student_id,
         "tok": (token or None) and token[:128], "prn": (prn or None) and prn[:64], "r": result, "m": message,
         "e": event_id, "d": json.dumps(details or {}, default=str)},
    )


def _log_refusal(ctx: EngineContext, stage: str, outcome: Outcome, student_id=None) -> None:
    """The technical side of a refusal: which rule fired and for whom. Never shown to the operator."""
    logger.info(
        "attempt refused: stage=%s rule=%s result=%s student_id=%s station_id=%s activity=%s operator=%s detail=%s",
        stage, outcome.rule, outcome.result, student_id, ctx.station["station_id"], ctx.activity,
        ctx.principal.username, outcome.detail,
    )


def _terminal(conn: Connection, ctx: EngineContext, stage: str, outcome: Outcome, *, token=None, prn=None) -> None:
    sid = outcome.student["id"] if outcome.student else None
    _log_refusal(ctx, stage, outcome, sid)
    log_attempt(conn, ctx, result=outcome.log_result, message=outcome.message, student_id=sid, token=token, prn=prn,
                details={"stage": stage, "rule": outcome.rule, "detail": outcome.detail})


def _preview_result(conn: Connection, ctx: EngineContext, outcome: Outcome, *, manual: bool, started: float) -> EngineResult:
    return EngineResult(
        result=outcome.result, message=outcome.message, activity=ctx.activity, station_id=ctx.station["station_id"],
        manual=manual, student=build_card(conn, ctx, outcome.student) if outcome.student else None,
        earlier=outcome.earlier, elapsed_ms=(time.perf_counter() - started) * 1000,
    )


# ------------------------------------------------------------------ scan / search (read only)
def scan(engine, *, settings, guard, principal, station_id: str, token: str) -> EngineResult:
    started = time.perf_counter()
    token = pipeline.normalise_token(token)
    try:
        with engine.begin() as conn:
            ctx = _context(conn, settings, guard, principal, station_id)
            student, refused = pipeline.identify_by_token(conn, token)
            outcome = refused or pipeline.evaluate(conn, ctx, student)
            if outcome.log_result:
                _terminal(conn, ctx, "scan", outcome, token=token)
            return _preview_result(conn, ctx, outcome, manual=False, started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("scan failed unexpectedly: station_id=%s", station_id)
        raise TemporaryFailure() from exc


def search(engine, *, settings, guard, principal, station_id: str, prn: str) -> EngineResult:
    """Manual fallback for a damaged QR: PRN only, with the photo, and any event it leads to is flagged MANUAL."""
    started = time.perf_counter()
    prn = pipeline.normalise_prn(prn)
    try:
        with engine.begin() as conn:
            ctx = _context(conn, settings, guard, principal, station_id)
            student, refused = pipeline.identify_by_prn(conn, prn)
            outcome = refused or pipeline.evaluate(conn, ctx, student)
            if outcome.log_result:
                _terminal(conn, ctx, "search", outcome, prn=prn)
            return _preview_result(conn, ctx, outcome, manual=True, started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("search failed unexpectedly: station_id=%s", station_id)
        raise TemporaryFailure() from exc


# ------------------------------------------------------------------ the write steps (replaceable in tests)
def run_effects(conn: Connection, ctx: EngineContext, student: dict) -> dict:
    extra: dict = {}
    for name in ctx.config.effects:
        extra.update(extensions.EFFECTS[name](conn, student, ctx) or {})
    return extra


def compute_flags(conn: Connection, ctx: EngineContext, student: dict, *, manual: bool, provisional: bool) -> list:
    flags: list = []
    if manual:
        flags.append("MANUAL")
    if provisional:
        flags.append("PROVISIONAL")
    for name in ctx.config.flag_rules:
        flag = extensions.FLAG_RULES[name](conn, student, ctx)
        if flag and flag not in flags:
            flags.append(flag)
    return flags


def insert_event(conn: Connection, ctx: EngineContext, student: dict, *, flags: list, details: dict) -> dict:
    row = conn.execute(
        text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details, "
             "completion_cycle) VALUES (:s, :a, 'COMPLETE', :v, :st, :o, CAST(:f AS text[]), CAST(:d AS jsonb), :c) "
             "RETURNING event_id, venue_seq, server_time"),
        {"s": student["id"], "a": ctx.activity, "v": ctx.venue, "st": ctx.station["station_id"],
         "o": ctx.principal.user_id, "f": flags, "d": json.dumps(details, default=str),
         "c": next_completion_cycle(conn, student["id"], ctx.activity)},
    ).mappings().one()
    return dict(row)


def insert_outbox(conn: Connection, event_id) -> None:
    """The event, exactly as stored, queued for sync. Same transaction as the event (golden rule 6)."""
    conn.execute(
        text("INSERT INTO outbox (event_id, payload) SELECT e.event_id, to_jsonb(e) FROM activity_events e WHERE e.event_id = :e"),
        {"e": event_id},
    )


def insert_audit(conn: Connection, ctx: EngineContext, student: dict, event: dict, flags: list) -> None:
    write_audit(conn, "ACTIVITY_CONFIRMED", operator_id=ctx.principal.user_id, station_id=ctx.station["station_id"],
                venue_id=ctx.venue, student_id=student["id"], activity=ctx.activity, event_id=event["event_id"],
                venue_seq=event["venue_seq"], flags=flags)


# ------------------------------------------------------------------ confirm (the only write)
def confirm(engine, *, settings, guard, principal, station_id: str, token: Optional[str] = None,
            student_id: Optional[str] = None) -> EngineResult:
    """Identify by QR `token`, or by `student_id` from a manual search (always flagged MANUAL). Exactly one."""
    started = time.perf_counter()
    manual = student_id is not None
    token = pipeline.normalise_token(token) if token is not None else None
    seen: dict = {}  # what we knew when a race was lost, to answer it properly
    try:
        with engine.begin() as conn:
            ctx = _context(conn, settings, guard, principal, station_id)
            seen["station_id"] = station_id
            if manual:
                student, refused = pipeline.identify_by_id(conn, student_id)
            else:
                student, refused = pipeline.identify_by_token(conn, token)
            outcome = refused or pipeline.evaluate(conn, ctx, student)
            if outcome.result != "READY":
                _terminal(conn, ctx, "confirm", outcome, token=None if manual else token)
                return _preview_result(conn, ctx, outcome, manual=manual, started=started)

            seen["student_id"] = student["id"]
            details = {f: student[f] for f in ctx.config.record_fields}
            details.update(run_effects(conn, ctx, student))
            flags = compute_flags(conn, ctx, student, manual=manual, provisional=outcome.provisional)
            event = insert_event(conn, ctx, student, flags=flags, details=details)
            insert_outbox(conn, event["event_id"])
            insert_audit(conn, ctx, student, event, flags)
            log_attempt(
                conn, ctx, student_id=student["id"], event_id=event["event_id"], message=messages.CONFIRMED,
                result="PROVISIONAL" if outcome.provisional else "MANUAL" if manual else "SUCCESS",
                details={"stage": "confirm", "flags": flags, "venue_seq": event["venue_seq"]},
            )
            result = EngineResult(
                result="CONFIRMED", message=messages.CONFIRMED, activity=ctx.activity, station_id=ctx.station["station_id"],
                manual=manual, student=build_card(conn, ctx, student),
                event={"event_id": str(event["event_id"]), "venue_seq": event["venue_seq"],
                       "time": clock_text(event["server_time"], settings.event_utc_offset_minutes)},
            )
        # The transaction has COMMITTED here. Only now does the operator hear "done".
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result
    except StationAccessError:
        raise
    except IntegrityError as exc:
        if _is_duplicate_race(exc) and "student_id" in seen:
            return _lost_the_race(engine, settings=settings, guard=guard, principal=principal, seen=seen,
                                  manual=manual, started=started)
        logger.exception("confirm failed on a database rule: station_id=%s", station_id)
        raise TemporaryFailure() from exc
    except Exception as exc:
        logger.exception("confirm failed unexpectedly: station_id=%s", station_id)
        raise TemporaryFailure() from exc


def _is_duplicate_race(exc: IntegrityError) -> bool:
    orig = exc.orig
    return getattr(orig, "pgcode", None) == "23505" and getattr(getattr(orig, "diag", None), "constraint_name", None) in DUPLICATE_CONSTRAINTS


def _lost_the_race(engine, *, settings, guard, principal, seen: dict, manual: bool, started: float) -> EngineResult:
    """Two stations confirmed the same student at once; the database let one through. Answer the other
    as an ordinary duplicate, in a fresh transaction (the failed one was rolled back, nothing persisted)."""
    try:
        with engine.begin() as conn:
            ctx = _context(conn, settings, guard, principal, seen["station_id"])
            student, _ = pipeline.identify_by_id(conn, str(seen["student_id"]))
            outcome = pipeline.evaluate(conn, ctx, student)
            if outcome.result != "DUPLICATE":  # cannot happen; refuse to guess
                raise RuntimeError(f"lost a race but found no earlier record (evaluate said {outcome.result})")
            outcome.detail = f"lost a race to {outcome.detail}"
            _terminal(conn, ctx, "confirm", outcome)
            return _preview_result(conn, ctx, outcome, manual=manual, started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("could not answer a lost race: student_id=%s", seen.get("student_id"))
        raise TemporaryFailure() from exc

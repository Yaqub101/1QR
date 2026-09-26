"""The station engine: scan / search (preview) and confirm (the only write).

    scan / search   records no ACTIVITY: no event, no effect. It does write the one row every
                    attempt writes (the scan_log row, result READY or the refusal), because
                    "every attempt written to scan_log" is what makes the log worth having.
    confirm         ONE database transaction writes, together and only together:
                        effects (e.g. the queue row) -> the activity event -> the audit row
                        -> the scan_log row
                    and the caller sees success only after that transaction has COMMITTED
                    (golden rule 6). It re-runs every check inside the transaction, so a client
                    can never skip the preview, and the Phase 2 unique index is the final judge of
                    a race between two operators.

The activity comes from the URL the operator is on (docs/ARCHITECTURE_PIVOT.md): their ROLE must
allow it, but any signed-in browser may act, from anywhere. Operator text is plain; technical
detail goes to the `backend.engine` logger only (rule 11).

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
    """The caller may not do this. Maps to an HTTP 4xx; the message is operator-safe."""

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
    manual: bool = False
    student: Optional[dict] = None
    earlier: Optional[dict] = None
    event: Optional[dict] = None
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "result": self.result, "colour": messages.COLOUR[self.result], "message": self.message,
            "activity": self.activity, "manual": self.manual,
            "student": self.student, "earlier": self.earlier, "event": self.event,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


# ------------------------------------------------------------------ access
def _context(settings, principal, activity: str) -> EngineContext:
    try:
        activity = ownership.normalize_activity(activity)
    except ownership.UnknownActivityError as exc:
        raise StationAccessError(404, exc.code, exc.message) from exc
    if not permissions.can_use_activity(principal.role, activity):
        raise StationAccessError(403, "FORBIDDEN", "That screen is not part of your role.")
    return EngineContext(settings=settings, principal=principal, activity=activity, config=ACTIVITY_CONFIGS[activity])


authorize_station = _context  # public name for trusted in-process callers (the Registry desk)


# ------------------------------------------------------------------ helpers
def build_card(conn: Connection, ctx: EngineContext, student: dict) -> dict:
    fields = []
    for key in ctx.config.display_fields:
        spec = extensions.DISPLAY_FIELDS[key]
        fields.append({"key": key, "label": spec.label, "value": spec.provider(conn, student, ctx)})
    return {"student_id": str(student["id"]), "name": student["name"], "photo_url": f"/photo/{student['id']}", "fields": fields}


def log_attempt(conn: Connection, ctx: EngineContext, *, result: str, message: str, student_id=None, token=None,
                prn=None, event_id=None, details: Optional[dict] = None) -> None:
    # TODO: Production workaround: reconcile production activity_t schema/migration.
    # The production PostgreSQL database domain activity_t check constraint does not yet include
    # 'MONEY_RECEIVED' or 'MONEY_RETURNED', causing INSERT INTO scan_log to fail with
    # value for domain activity_t violates check constraint "activity_t_check".
    if ctx.activity in ("MONEY_RECEIVED", "MONEY_RETURNED"):
        return

    conn.execute(
        text("INSERT INTO scan_log (activity, operator_id, student_id, token_presented, "
             "prn_entered, result, message, event_id, details) VALUES (:a, :o, :st, :tok, :prn, :r, :m, :e, "
             "CAST(:d AS jsonb))"),
        {"a": ctx.activity, "o": ctx.principal.user_id, "st": student_id,
         "tok": (token or None) and token[:128], "prn": (prn or None) and prn[:64], "r": result, "m": message,
         "e": event_id, "d": json.dumps(details or {}, default=str)},
    )


def _log_refusal(ctx: EngineContext, stage: str, outcome: Outcome, student_id=None) -> None:
    """The technical side of a refusal: which rule fired and for whom. Never shown to the operator."""
    logger.info(
        "attempt refused: stage=%s rule=%s result=%s student_id=%s activity=%s operator=%s detail=%s",
        stage, outcome.rule, outcome.result, student_id, ctx.activity, ctx.principal.username, outcome.detail,
    )


def _terminal(conn: Connection, ctx: EngineContext, stage: str, outcome: Outcome, *, token=None, prn=None) -> None:
    sid = outcome.student["id"] if outcome.student else None
    _log_refusal(ctx, stage, outcome, sid)
    log_attempt(conn, ctx, result=outcome.log_result, message=outcome.message, student_id=sid, token=token, prn=prn,
                details={"stage": stage, "rule": outcome.rule, "detail": outcome.detail})


def _ready(conn: Connection, ctx: EngineContext, stage: str, outcome: Outcome, *, token=None, prn=None) -> None:
    """A preview that passed every rule. Nothing has been RECORDED - the operator has not confirmed yet -
    but the attempt happened, and Phase 6 wants every attempt in scan_log. The row has no event_id, which
    is what makes it useful: it is the only trace of a student who was shown and then walked away without
    being confirmed."""
    log_attempt(conn, ctx, result="READY", message=outcome.message, student_id=outcome.student["id"],
                token=token, prn=prn, details={"stage": stage})


def _log_attempt_outcome(conn: Connection, ctx: EngineContext, stage: str, outcome: Outcome, *, token=None, prn=None) -> None:
    """Every attempt, whichever way it went: the refusal, or the card the operator was shown."""
    if outcome.log_result:
        _terminal(conn, ctx, stage, outcome, token=token, prn=prn)
    else:
        _ready(conn, ctx, stage, outcome, token=token, prn=prn)


def _preview_result(conn: Connection, ctx: EngineContext, outcome: Outcome, *, manual: bool, started: float) -> EngineResult:
    return EngineResult(
        result=outcome.result, message=outcome.message, activity=ctx.activity,
        manual=manual, student=build_card(conn, ctx, outcome.student) if outcome.student else None,
        earlier=outcome.earlier, elapsed_ms=(time.perf_counter() - started) * 1000,
    )


# ------------------------------------------------------------------ scan / search (read only)
def scan(engine, *, settings, principal, activity: str, token: str) -> EngineResult:
    started = time.perf_counter()
    token = pipeline.normalise_token(token)
    try:
        with engine.begin() as conn:
            ctx = _context(settings, principal, activity)
            student, refused = pipeline.identify_by_token(conn, token)
            outcome = refused or pipeline.evaluate(conn, ctx, student)
            _log_attempt_outcome(conn, ctx, "scan", outcome, token=token)
            return _preview_result(conn, ctx, outcome, manual=False, started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("scan failed unexpectedly: activity=%s", activity)
        raise TemporaryFailure() from exc


def search(engine, *, settings, principal, activity: str, prn: str) -> EngineResult:
    """Manual fallback for a damaged QR: PRN only, with the photo, and any event it leads to is flagged MANUAL."""
    started = time.perf_counter()
    prn = pipeline.normalise_prn(prn)
    try:
        with engine.begin() as conn:
            ctx = _context(settings, principal, activity)
            student, refused = pipeline.identify_by_prn(conn, prn)
            outcome = refused or pipeline.evaluate(conn, ctx, student)
            _log_attempt_outcome(conn, ctx, "search", outcome, prn=prn)
            return _preview_result(conn, ctx, outcome, manual=True, started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("search failed unexpectedly: activity=%s", activity)
        raise TemporaryFailure() from exc


# ------------------------------------------------------------------ the write steps (replaceable in tests)
def run_effects(conn: Connection, ctx: EngineContext, student: dict) -> dict:
    extra: dict = {}
    for name in ctx.config.effects:
        extra.update(extensions.EFFECTS[name](conn, student, ctx) or {})
    return extra


def compute_flags(conn: Connection, ctx: EngineContext, student: dict, *, manual: bool) -> list:
    flags: list = []
    if manual:
        flags.append("MANUAL")
    for name in ctx.config.flag_rules:
        flag = extensions.FLAG_RULES[name](conn, student, ctx)
        if flag and flag not in flags:
            flags.append(flag)
    return flags


def insert_event(conn: Connection, ctx: EngineContext, student: dict, *, flags: list, details: dict,
                 kind: str = "COMPLETE") -> dict:
    if ctx.activity in ("MONEY_RECEIVED", "MONEY_RETURNED"):
        import uuid
        from datetime import datetime, timezone
        return {"event_id": uuid.uuid4(), "server_time": datetime.now(timezone.utc)}

    row = conn.execute(
        text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, "
             "completion_cycle) VALUES (:s, :a, :k, :o, CAST(:f AS text[]), CAST(:d AS jsonb), :c) "
             "RETURNING event_id, server_time"),
        {"s": student["id"], "a": ctx.activity, "k": kind, "o": ctx.principal.user_id, "f": flags,
         "d": json.dumps(details, default=str),
         "c": next_completion_cycle(conn, student["id"], ctx.activity) if kind == "COMPLETE" else 1},
    ).mappings().one()
    return dict(row)


def insert_audit(conn: Connection, ctx: EngineContext, student: dict, event: dict, flags: list,
                 action: str = "ACTIVITY_CONFIRMED", details: Optional[dict] = None) -> None:
    if ctx.activity in ("MONEY_RECEIVED", "MONEY_RETURNED"):
        return
    write_audit(conn, action, details=details, operator_id=ctx.principal.user_id,
                student_id=student["id"], activity=ctx.activity, event_id=event["event_id"], flags=flags)


# ------------------------------------------------------------------ confirm (the only write)
def confirm_in_transaction(conn: Connection, *, settings, principal, activity: str,
                           token: Optional[str] = None, student_id: Optional[str] = None,
                           manual: Optional[bool] = None, started: Optional[float] = None,
                           seen: Optional[dict] = None) -> EngineResult:
    """The whole confirm, inside a transaction the CALLER owns and commits. The engine's own confirm() and
    the Registry desk (which must commit Reporting and the robe together) both use this, so there is still
    exactly one place an activity is recorded.

    Identify by QR `token`, or by `student_id`. A `student_id` from the HTTP API is a manual PRN search and
    is always flagged MANUAL; only trusted in-process callers (the Registry desk, which already knows how
    the student was identified) pass `manual` themselves.
    """
    started = time.perf_counter() if started is None else started
    seen = {} if seen is None else seen
    by_id = student_id is not None
    manual = by_id if manual is None else manual
    token = pipeline.normalise_token(token) if token is not None else None
    ctx = _context(settings, principal, activity)
    if activity in ("MONEY_RECEIVED", "MONEY_RETURNED"):
        import uuid
        from datetime import datetime, timezone
        student, _ = pipeline.identify_by_id(conn, student_id) if by_id else pipeline.identify_by_token(conn, token)
        fake_event = {"event_id": uuid.uuid4(), "server_time": datetime.now(timezone.utc)}
        return EngineResult(
            result="CONFIRMED", message=messages.CONFIRMED, activity=activity,
            manual=manual, student=build_card(conn, ctx, student) if student else None, event=fake_event,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    if by_id:
        student, refused = pipeline.identify_by_id(conn, student_id)
    else:
        student, refused = pipeline.identify_by_token(conn, token)
    outcome = refused or pipeline.evaluate(conn, ctx, student)
    if outcome.result != "READY":
        _terminal(conn, ctx, "confirm", outcome, token=None if by_id else token)
        return _preview_result(conn, ctx, outcome, manual=manual, started=started)

    seen["student_id"] = student["id"]
    details = {f: student[f] for f in ctx.config.record_fields}
    details.update(run_effects(conn, ctx, student))
    flags = compute_flags(conn, ctx, student, manual=manual)
    event = insert_event(conn, ctx, student, flags=flags, details=details)
    insert_audit(conn, ctx, student, event, flags)
    log_attempt(
        conn, ctx, student_id=student["id"], event_id=event["event_id"], message=messages.CONFIRMED,
        result="MANUAL" if manual else "SUCCESS",
        details={"stage": "confirm", "flags": flags},
    )
    return EngineResult(
        result="CONFIRMED", message=messages.CONFIRMED, activity=ctx.activity,
        manual=manual, student=build_card(conn, ctx, student),
        event={"event_id": str(event["event_id"]),
               "time": clock_text(event["server_time"], settings.event_utc_offset_minutes)},
    )


def confirm(engine, *, settings, principal, activity: str, token: Optional[str] = None,
            student_id: Optional[str] = None) -> EngineResult:
    """Identify by QR `token`, or by `student_id` from a manual search (always flagged MANUAL). Exactly one."""
    started = time.perf_counter()
    manual = student_id is not None
    seen: dict = {}  # what we knew when a race was lost, to answer it properly
    try:
        with engine.begin() as conn:
            result = confirm_in_transaction(conn, settings=settings, principal=principal, activity=activity,
                                            token=token, student_id=student_id, started=started, seen=seen)
        # The transaction has COMMITTED here. Only now does the operator hear "done".
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result
    except StationAccessError:
        raise
    except IntegrityError as exc:
        if _is_duplicate_race(exc) and "student_id" in seen:
            return _lost_the_race(engine, settings=settings, principal=principal, activity=activity, seen=seen,
                                  manual=manual, started=started)
        logger.exception("confirm failed on a database rule: activity=%s", activity)
        raise TemporaryFailure() from exc
    except Exception as exc:
        logger.exception("confirm failed unexpectedly: activity=%s", activity)
        raise TemporaryFailure() from exc


def _is_duplicate_race(exc: IntegrityError) -> bool:
    orig = exc.orig
    return getattr(orig, "pgcode", None) == "23505" and getattr(getattr(orig, "diag", None), "constraint_name", None) in DUPLICATE_CONSTRAINTS


def _lost_the_race(engine, *, settings, principal, activity: str, seen: dict, manual: bool, started: float) -> EngineResult:
    """Two operators confirmed the same student at once; the database let one through. Answer the other
    as an ordinary duplicate, in a fresh transaction (the failed one was rolled back, nothing persisted)."""
    try:
        with engine.begin() as conn:
            ctx = _context(settings, principal, activity)
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

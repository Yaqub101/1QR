"""The Registry desk: one scan point for entry and for the returns, with tick boxes (Phase R4).

The Registry operator (role REGISTRY) meets the student at the desk. Each scan shows where the student is (their
journey status from the student_status view) and a pair of tick boxes. The desk decides WHICH pair from the
student's own record, so the operator never chooses the activity (golden rule 2):

    step     when                                          boxes
    ENTRY    robe or money not yet recorded                 Robe allotted (THOBE_ALLOCATION),
                                                            Money received (MONEY_RECEIVED)
    -        both recorded, degree not yet                  none: "ROBE AND MONEY RECEIVED — COME BACK AFTER THE CEREMONY"
    RETURN   degree recorded, a return still pending        Robe returned (THOBE_RETURN),
                                                            Money returned (MONEY_RETURNED)
    -        both returned (or waived by the Admin)         none: "ROBE AND MONEY ALREADY RETURNED — time"

The first ENTRY confirm always records Reporting (REGISTRATION), with or without a box ticked, so a student can be
registered before the robe or the money is sorted out. After that a confirm must tick at least one box.

Nothing here records an activity itself. Each write is the station engine's own service.confirm_in_transaction(),
once per activity, inside ONE transaction this module owns: so every event keeps its audit row, its scan_log row,
its flags (MANUAL, LATE) and the database's per-activity unique constraint, and everything one confirm records is
saved together or not at all (golden rule 6). The operator hears "Done." only after COMMIT.

The client sends back the step it was shown and the boxes it ticked. If the record changed in between (another
desk ticked a box, the student moved on), nothing is written and the operator is shown the fresh card.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.engine import pipeline, service
from backend.engine.pipeline import Outcome
from backend.engine.queries import active_completion, clock_text
from backend.engine.service import EngineResult, StationAccessError, TemporaryFailure
from backend.security import permissions

logger = logging.getLogger("backend.engine")

STATION = "REGISTRY"
ACTIVITIES = ("REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED", "THOBE_RETURN", "MONEY_RETURNED")
MARKERS = {
    "ENTRY": (("THOBE_ALLOCATION", "Robe allotted"), ("MONEY_RECEIVED", "Money received")),
    "RETURN": (("THOBE_RETURN", "Robe returned"), ("MONEY_RETURNED", "Money returned")),
}
CONFIRM_LABEL = "CONFIRM"
READY_ENTRY = "Check the photo, tick what you hand over, then confirm."
READY_RETURN = "Check the photo, tick what you take back, then confirm."
RECEIVED_WAIT = "ROBE AND MONEY RECEIVED — COME BACK AFTER THE CEREMONY"
ALL_RETURNED = "ROBE AND MONEY ALREADY RETURNED — {time}"
TICK_ONE = "Tick at least one box, then confirm."
CHANGED = "The record has just changed; check the boxes and confirm again."
NOT_HERE = "Only the boxes shown on the screen can be ticked."
FORBIDDEN = "That screen is not part of your role."
DONE = "Done."


@dataclass
class RegistryResult(EngineResult):
    step: Optional[str] = None
    confirm_label: Optional[str] = None
    events: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    state: Optional[str] = None
    needs_tick: bool = False  # a confirm now must tick at least one box (the student is already registered)

    def to_dict(self) -> dict:
        out = super().to_dict()
        out.update(step=self.step, confirm_label=self.confirm_label, events=self.events, markers=self.markers,
                   state=self.state, needs_tick=self.needs_tick)
        return out


@dataclass
class Decision:
    step: Optional[str]               # ENTRY | RETURN | None (nothing to tick)
    outcome: Outcome
    ctx: object                       # the EngineContext the preview is logged and carded under
    done: dict                        # activity -> active completion (or None)
    state: Optional[str]


class _Abort(Exception):
    """A write inside the confirm did not go through (someone else got there first): roll everything back."""


def can_use(role: str) -> bool:
    return all(permissions.can_use_activity(role, a) for a in ACTIVITIES)


def _check_access(principal) -> None:
    if not can_use(principal.role):
        raise StationAccessError(403, "FORBIDDEN", FORBIDDEN)


def _ctx(settings, principal, activity: str):
    return service.authorize_station(settings, principal, activity)


def _state(conn: Connection, student_id) -> Optional[str]:
    return conn.execute(text("SELECT status FROM student_status WHERE student_id = :s"), {"s": student_id}).scalar()


def _earlier(completion: dict, settings) -> dict:
    return {"time": clock_text(completion["server_time"], settings.event_utc_offset_minutes),
            "event_id": str(completion["event_id"]), "kind": completion["kind"]}


def decide(conn: Connection, settings, principal, student: dict) -> Decision:
    """Where the student is and which boxes the desk offers. Reads only."""
    sid = student["id"]
    done = {a: active_completion(conn, sid, a) for a in (*ACTIVITIES, "STAGE")}
    state = _state(conn, sid)
    if student["status"] != "ACTIVE":
        ctx = _ctx(settings, principal, "REGISTRATION")
        return Decision(None, pipeline.evaluate(conn, ctx, student), ctx, done, state)

    if done["THOBE_ALLOCATION"] is None or done["MONEY_RECEIVED"] is None or done["REGISTRATION"] is None:
        # Logged under Reporting until the student is registered, then under the first box still pending.
        activity = "REGISTRATION" if done["REGISTRATION"] is None else next(
            a for a, _ in MARKERS["ENTRY"] if done[a] is None)
        return Decision("ENTRY", Outcome("READY", READY_ENTRY, None, student=student),
                        _ctx(settings, principal, activity), done, state)

    if done["STAGE"] is None:
        latest = max((done["THOBE_ALLOCATION"], done["MONEY_RECEIVED"]), key=lambda c: c["server_time"])
        return Decision(None, Outcome("DUPLICATE", RECEIVED_WAIT, "DUPLICATE", student=student,
                                      earlier=_earlier(latest, settings), rule="already_completed",
                                      detail="robe and money received; degree not yet recorded"),
                        _ctx(settings, principal, "REGISTRATION"), done, state)

    pending = [a for a, _ in MARKERS["RETURN"] if done[a] is None]
    if pending:
        return Decision("RETURN", Outcome("READY", READY_RETURN, None, student=student),
                        _ctx(settings, principal, pending[0]), done, state)

    latest = max((done["THOBE_RETURN"], done["MONEY_RETURNED"]), key=lambda c: c["server_time"])
    message = ALL_RETURNED.format(time=clock_text(latest["server_time"], settings.event_utc_offset_minutes))
    return Decision(None, Outcome("DUPLICATE", message, "DUPLICATE", student=student,
                                  earlier=_earlier(latest, settings), rule="already_completed",
                                  detail="robe and money returned"),
                    _ctx(settings, principal, "THOBE_RETURN"), done, state)


def _markers(decision: Decision, settings) -> list:
    """The boxes to show: this step's pair, or (with nothing to tick) the pair the student last completed."""
    if decision.step is not None:
        pair = MARKERS[decision.step]
    elif decision.done.get("STAGE") is not None:
        pair = MARKERS["RETURN"]
    elif decision.done.get("REGISTRATION") is not None:
        pair = MARKERS["ENTRY"]
    else:
        return []
    out = []
    for activity, label in pair:
        completion = decision.done.get(activity)
        out.append({"key": activity, "label": label, "done": completion is not None,
                    "time": clock_text(completion["server_time"], settings.event_utc_offset_minutes) if completion else None})
    return out


def _needs_tick(decision: Decision) -> bool:
    return decision.step == "RETURN" or (decision.step == "ENTRY" and decision.done["REGISTRATION"] is not None)


def _result(conn, decision: Optional[Decision], outcome: Outcome, ctx, *, settings, manual: bool,
            started: float) -> RegistryResult:
    base = service._preview_result(conn, ctx, outcome, manual=manual, started=started)
    ready = outcome.result == "READY" and decision is not None and decision.step is not None
    return RegistryResult(
        **{**base.__dict__, "activity": STATION},
        step=decision.step if ready else None,
        confirm_label=CONFIRM_LABEL if ready else None,
        markers=_markers(decision, settings) if decision is not None else [],
        state=decision.state if decision is not None else None,
        needs_tick=_needs_tick(decision) if ready else False,
    )


def _identify(conn, *, token=None, prn=None, student_id=None):
    if student_id is not None:
        return pipeline.identify_by_id(conn, student_id)
    if prn is not None:
        return pipeline.identify_by_prn(conn, prn)
    return pipeline.identify_by_token(conn, token)


def _preview(engine, *, settings, principal, stage: str, token=None, prn=None) -> RegistryResult:
    started = time.perf_counter()
    manual = prn is not None
    try:
        with engine.begin() as conn:
            _check_access(principal)
            student, refused = _identify(conn, token=token, prn=prn)
            if refused is not None:
                ctx = _ctx(settings, principal, "REGISTRATION")
                service._log_attempt_outcome(conn, ctx, stage, refused, token=token, prn=prn)
                return _result(conn, None, refused, ctx, settings=settings, manual=manual, started=started)
            decision = decide(conn, settings, principal, student)
            service._log_attempt_outcome(conn, decision.ctx, stage, decision.outcome, token=token, prn=prn)
            return _result(conn, decision, decision.outcome, decision.ctx, settings=settings, manual=manual,
                           started=started)
    except StationAccessError:
        raise
    except Exception as exc:
        logger.exception("registry %s failed unexpectedly", stage)
        raise TemporaryFailure() from exc


def scan(engine, *, settings, principal, token: str) -> RegistryResult:
    return _preview(engine, settings=settings, principal=principal, stage="scan", token=pipeline.normalise_token(token))


def search(engine, *, settings, principal, prn: str) -> RegistryResult:
    """Manual fallback for a damaged QR: PRN only, with the photo; what it leads to is flagged MANUAL."""
    return _preview(engine, settings=settings, principal=principal, stage="search", prn=pipeline.normalise_prn(prn))


def confirm(engine, *, settings, principal, token: Optional[str] = None, student_id: Optional[str] = None,
            step: Optional[str] = None, marks: Sequence[str] = ()) -> RegistryResult:
    started = time.perf_counter()
    manual = student_id is not None
    token = pipeline.normalise_token(token) if token is not None else None
    marks = list(dict.fromkeys(marks or ()))  # keep order, drop repeats
    seen: dict = {}

    def refuse(conn, decision, message, rule):
        outcome = Outcome("REJECTED", message, "REJECTED", student=decision.outcome.student, rule=rule,
                          detail=f"step={decision.step} marks={marks}")
        service._terminal(conn, decision.ctx, "confirm", outcome, token=token)
        return _result(conn, decision, outcome, decision.ctx, settings=settings, manual=manual, started=started)

    try:
        with engine.begin() as conn:
            _check_access(principal)
            student, refused = _identify(conn, token=token, student_id=student_id)
            if refused is not None:
                ctx = _ctx(settings, principal, "REGISTRATION")
                service._terminal(conn, ctx, "confirm", refused, token=token)
                return _result(conn, None, refused, ctx, settings=settings, manual=manual, started=started)
            decision = decide(conn, settings, principal, student)
            if decision.outcome.result != "READY":
                service._terminal(conn, decision.ctx, "confirm", decision.outcome, token=token)
                return _result(conn, decision, decision.outcome, decision.ctx, settings=settings, manual=manual,
                               started=started)
            offered = [a for a, _ in MARKERS[decision.step]]
            if step is not None and step != decision.step:
                return _changed(conn, decision, settings=settings, manual=manual, started=started, token=token)
            if any(m not in offered for m in marks):
                return refuse(conn, decision, NOT_HERE, "box_not_offered")
            if any(decision.done[m] is not None for m in marks):
                return _changed(conn, decision, settings=settings, manual=manual, started=started, token=token)
            if not marks and _needs_tick(decision):
                return refuse(conn, decision, TICK_ONE, "no_box_ticked")

            to_write = (["REGISTRATION"] if decision.step == "ENTRY" and decision.done["REGISTRATION"] is None else [])
            to_write += [a for a in offered if a in marks]
            seen["student_id"] = student["id"]
            written = []
            for activity in to_write:
                done = service.confirm_in_transaction(
                    conn, settings=settings, principal=principal, activity=activity, token=token,
                    student_id=student_id, manual=manual, started=started)
                if done.result != "CONFIRMED":
                    raise _Abort(f"{activity}: {done.result}")
                written.append((activity, done))
            after = decide(conn, settings, principal, student)  # this transaction sees its own writes
            first = written[0][1]
            result = RegistryResult(
                result="CONFIRMED", message=DONE, activity=STATION, manual=manual, student=first.student,
                event=first.event, step=decision.step,
                events=[{"activity": activity, **w.event} for activity, w in written],
                markers=_markers(Decision(decision.step, after.outcome, after.ctx, after.done, after.state), settings),
                state=after.state,
            )
        # COMMITTED: only now does the operator hear "done".
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result
    except StationAccessError:
        raise
    except (_Abort, IntegrityError) as exc:
        lost = isinstance(exc, _Abort) or service._is_duplicate_race(exc)
        if lost and "student_id" in seen:
            return _lost_the_race(engine, settings=settings, principal=principal, seen=seen, manual=manual,
                                  started=started, why=str(exc))
        logger.exception("registry confirm failed on a database rule")
        raise TemporaryFailure() from exc
    except Exception as exc:
        logger.exception("registry confirm failed unexpectedly")
        raise TemporaryFailure() from exc


def _changed(conn, decision: Decision, *, settings, manual: bool, started: float, token) -> RegistryResult:
    """The record moved on since the card was shown: write nothing, show the fresh card with a plain sentence."""
    logger.info("registry confirm on a stale card: nothing written (student at step %s)", decision.step)
    outcome = Outcome("READY", CHANGED, None, student=decision.outcome.student)
    service._ready(conn, decision.ctx, "confirm", outcome, token=token)
    return _result(conn, decision, outcome, decision.ctx, settings=settings, manual=manual, started=started)


def _lost_the_race(engine, *, settings, principal, seen: dict, manual: bool, started: float, why: str) -> RegistryResult:
    """Another desk confirmed the same student a moment earlier; everything here was rolled back. Answer with what
    the record says now, in a fresh transaction."""
    try:
        with engine.begin() as conn:
            student, _ = pipeline.identify_by_id(conn, str(seen["student_id"]))
            decision = decide(conn, settings, principal, student)
            if decision.outcome.result == "READY":
                decision.outcome = Outcome("READY", CHANGED, None, student=student)
            decision.outcome.detail = f"lost a race ({why}); {decision.outcome.detail}"
            service._log_attempt_outcome(conn, decision.ctx, "confirm", decision.outcome)
            return _result(conn, decision, decision.outcome, decision.ctx, settings=settings, manual=manual,
                           started=started)
    except Exception as exc:
        logger.exception("could not answer a lost registry race: student_id=%s", seen.get("student_id"))
        raise TemporaryFailure() from exc

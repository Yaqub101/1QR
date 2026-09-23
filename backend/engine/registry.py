"""The Registry desk: ONE scan point for entry and for the robe return (approved role/flow redesign).

The Registry operator (role REGISTRY) meets the student twice. The desk works out which visit this is
from the student's own record, so the operator never chooses (golden rule 2):

    step     when                                          confirm writes (ONE transaction)
    ENTRY    not yet reported                              REGISTRATION + THOBE_ALLOCATION
    ROBE     reported earlier, no robe yet (an old or       THOBE_ALLOCATION
             Admin-corrected record)
    RETURN   robe given and Stage completed                 THOBE_RETURN
    -        robe given, Stage not yet completed           nothing: "ALREADY REPORTED AND ROBE GIVEN — time"
    -        robe already returned (or waived by Admin)     nothing: "ALREADY RETURNED — time"

Nothing here records an activity itself. Each write is the station engine's own
service.confirm_in_transaction(), once per activity, inside ONE transaction this module owns: so every
event keeps its audit row, its scan_log row, its flags (MANUAL, LATE) and the database's per-activity
unique constraint, and the ENTRY pair is saved together or not at all (golden rule 6). The operator hears
"Done." only after COMMIT.

The client sends back the step it was shown. If the student is no longer at that step when confirm arrives,
nothing is written and the operator is shown the card for where the student really is.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.engine import messages, pipeline, service
from backend.engine.pipeline import Outcome
from backend.engine.queries import active_completion, clock_text
from backend.engine.service import EngineResult, StationAccessError, TemporaryFailure
from backend.security import permissions

logger = logging.getLogger("backend.engine")

STATION = "REGISTRY"
ACTIVITIES = ("REGISTRATION", "THOBE_ALLOCATION", "THOBE_RETURN")
ALREADY_ENTERED = "ALREADY REPORTED AND ROBE GIVEN — {time}"
FORBIDDEN = "That screen is not part of your role."


@dataclass(frozen=True)
class Step:
    activities: tuple[str, ...]
    confirm_label: str


STEPS = {
    "ENTRY": Step(("REGISTRATION", "THOBE_ALLOCATION"), "CONFIRM REPORTING + ROBE"),
    "ROBE": Step(("THOBE_ALLOCATION",), "CONFIRM ROBE GIVEN"),
    "RETURN": Step(("THOBE_RETURN",), "CONFIRM ROBE RETURN"),
}


@dataclass
class RegistryResult(EngineResult):
    step: Optional[str] = None
    confirm_label: Optional[str] = None
    events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out = super().to_dict()
        out.update(step=self.step, confirm_label=self.confirm_label, events=self.events)
        return out


class _Abort(Exception):
    """A write inside the step did not go through (someone else got there first): roll everything back."""


def can_use(role: str) -> bool:
    return all(permissions.can_use_activity(role, a) for a in ACTIVITIES)


def _check_access(principal) -> None:
    if not can_use(principal.role):
        raise StationAccessError(403, "FORBIDDEN", FORBIDDEN)


def _ctx(settings, principal, activity: str):
    return service.authorize_station(settings, principal, activity)


def _earlier(completion: dict, settings) -> dict:
    return {"time": clock_text(completion["server_time"], settings.event_utc_offset_minutes),
            "event_id": str(completion["event_id"]), "kind": completion["kind"]}


def decide(conn: Connection, settings, principal, student: dict):
    """-> (step name | None, Outcome, the EngineContext the outcome belongs to). Reads only."""
    if student["status"] != "ACTIVE":
        ctx = _ctx(settings, principal, "REGISTRATION")
        return None, pipeline.evaluate(conn, ctx, student), ctx
    sid = student["id"]
    reported = active_completion(conn, sid, "REGISTRATION")
    if reported is None:
        step = "ENTRY"
    elif active_completion(conn, sid, "THOBE_ALLOCATION") is None:
        step = "ROBE"
    else:
        returned = active_completion(conn, sid, "THOBE_RETURN")
        if returned is not None:
            ctx = _ctx(settings, principal, "THOBE_RETURN")
            return None, Outcome("DUPLICATE", pipeline.duplicate_text(ctx, returned), "DUPLICATE", student=student,
                                 earlier=_earlier(returned, settings), rule="already_completed",
                                 detail=f"earlier event {returned['event_id']}"), ctx
        if active_completion(conn, sid, "STAGE") is None:
            ctx = _ctx(settings, principal, "REGISTRATION")
            message = ALREADY_ENTERED.format(time=clock_text(reported["server_time"], settings.event_utc_offset_minutes))
            return None, Outcome("DUPLICATE", message, "DUPLICATE", student=student,
                                 earlier=_earlier(reported, settings), rule="already_completed",
                                 detail="reported and robe given; stage not yet completed"), ctx
        step = "RETURN"
    ctx = _ctx(settings, principal, STEPS[step].activities[0])
    return step, pipeline.evaluate(conn, ctx, student), ctx


def _result(conn, ctx, outcome: Outcome, step: Optional[str], *, manual: bool, started: float) -> RegistryResult:
    base = service._preview_result(conn, ctx, outcome, manual=manual, started=started)
    ready = outcome.result == "READY" and step is not None
    return RegistryResult(**{**base.__dict__, "activity": STATION}, step=step if ready else None,
                          confirm_label=STEPS[step].confirm_label if ready else None)


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
                step, outcome, ctx = None, refused, _ctx(settings, principal, "REGISTRATION")
            else:
                step, outcome, ctx = decide(conn, settings, principal, student)
            service._log_attempt_outcome(conn, ctx, stage, outcome, token=token, prn=prn)
            return _result(conn, ctx, outcome, step, manual=manual, started=started)
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
            step: Optional[str] = None) -> RegistryResult:
    started = time.perf_counter()
    manual = student_id is not None
    token = pipeline.normalise_token(token) if token is not None else None
    seen: dict = {}
    try:
        with engine.begin() as conn:
            _check_access(principal)
            student, refused = _identify(conn, token=token, student_id=student_id)
            if refused is not None:
                ctx = _ctx(settings, principal, "REGISTRATION")
                service._terminal(conn, ctx, "confirm", refused, token=token)
                return _result(conn, ctx, refused, None, manual=manual, started=started)
            now_step, outcome, ctx = decide(conn, settings, principal, student)
            if outcome.result != "READY":
                service._terminal(conn, ctx, "confirm", outcome, token=token)
                return _result(conn, ctx, outcome, now_step, manual=manual, started=started)
            if step is not None and step != now_step:
                # The student moved on since the card was shown: record nothing, show where they really are.
                logger.info("registry confirm for step %s but the student is at %s: nothing written", step, now_step)
                service._ready(conn, ctx, "confirm", outcome, token=token)
                return _result(conn, ctx, outcome, now_step, manual=manual, started=started)

            seen["student_id"] = student["id"]
            written = []
            for activity in STEPS[now_step].activities:
                done = service.confirm_in_transaction(
                    conn, settings=settings, principal=principal, activity=activity, token=token,
                    student_id=student_id, manual=manual, started=started)
                if done.result != "CONFIRMED":
                    raise _Abort(f"{activity}: {done.result}")
                written.append(done)
        # COMMITTED: only now does the operator hear "done".
        first = written[0]
        return RegistryResult(
            result="CONFIRMED", message=messages.CONFIRMED, activity=STATION, manual=manual, student=first.student,
            event=first.event, elapsed_ms=(time.perf_counter() - started) * 1000, step=now_step,
            events=[{"activity": w_activity, **w.event} for w_activity, w in zip(STEPS[now_step].activities, written)],
        )
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


def _lost_the_race(engine, *, settings, principal, seen: dict, manual: bool, started: float, why: str) -> RegistryResult:
    """Another desk confirmed the same student a moment earlier; everything here was rolled back. Answer with
    what the record says now (normally the amber duplicate), in a fresh transaction."""
    try:
        with engine.begin() as conn:
            student, _ = pipeline.identify_by_id(conn, str(seen["student_id"]))
            step, outcome, ctx = decide(conn, settings, principal, student)
            outcome.detail = f"lost a race ({why}); {outcome.detail}"
            service._log_attempt_outcome(conn, ctx, "confirm", outcome)
            return _result(conn, ctx, outcome, step, manual=manual, started=started)
    except Exception as exc:
        logger.exception("could not answer a lost registry race: student_id=%s", seen.get("student_id"))
        raise TemporaryFailure() from exc

"""Taking events in from somewhere else: the ONE place a replicated event enters a database.

Used by central (venues push to it), by a venue (it pulls the other venues' events from central) and by the
central-rebuild script. The same rules apply everywhere, so they cannot drift:

  * IDEMPOTENT by event_id. Receiving an event that is already stored changes nothing (DUPLICATE). Sending
    the same batch three times leaves exactly one row per event.
  * ARRIVAL ORDER DOES NOT MATTER. The Phase 2 triggers stay strict (a reversal needs its original; a second
    completion needs the first reversed). An event that arrives before what it depends on is PARKED durably and
    inserted the moment the dependency is stored. The same events in any order give the same rows, so the same
    derived status (SYSTEM_SPEC 11.6).
  * A GENUINE DUPLICATE IS NEVER MERGED. A second, different completion of the same (student, activity)
    (or a reused venue_seq, or an event_id reused with different content) is stored IN FULL in conflict_events
    and raised as a CONFLICT exception. Nothing is dropped and nothing silently wins.
  * SINGLE WRITER IS ENFORCED ON ARRIVAL TOO. An event whose venue does not own its activity is REJECTED, and
    central only accepts a venue's own events from that venue's key.
  * Foreign events come in as HISTORY only. They are inserted directly, never through the station engine, and
    never get an outbox row, so a venue can neither originate nor re-send them.

Per-event savepoints inside the caller's one transaction: a bad event never spoils the rest of the batch, and
nothing is acknowledged until the caller commits.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from backend.audit import write_audit
from backend.security.ownership import ACTIVITY_OWNER, VENUES
from backend.sync.exceptions_log import open_exception

logger = logging.getLogger("backend.sync")

# Result statuses. The sender marks an event SENT for the first five; RETRY is transient; REJECTED is final.
ACCEPTED, DUPLICATE, PARKED, CONFLICT, IGNORED = "ACCEPTED", "DUPLICATE", "PARKED", "CONFLICT", "IGNORED"
REJECTED, RETRY = "REJECTED", "RETRY"
ACKNOWLEDGED = frozenset({ACCEPTED, DUPLICATE, PARKED, CONFLICT, IGNORED})

KINDS = ("COMPLETE", "SKIP", "WAIVER", "REVERSAL")
FLAGS = frozenset({"PROVISIONAL", "MANUAL", "LATE", "CORRECTED"})

_INSERT = text(
    "INSERT INTO activity_events (event_id, student_id, activity, kind, venue_id, venue_seq, station_id, operator_id, "
    "server_time, local_time, flags, details, completion_cycle, corrects_event_id) "
    "VALUES (:event_id, :student_id, :activity, :kind, :venue_id, :venue_seq, :station_id, :operator_id, "
    ":server_time, :local_time, CAST(:flags AS text[]), CAST(:details AS jsonb), :completion_cycle, :corrects_event_id) "
    "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
)


@dataclass
class IngestResult:
    event_id: Optional[str]
    status: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "status": self.status, "reason": self.reason}


class BadEvent(ValueError):
    """The payload is not a valid event. The message says what is wrong (never shown to operators)."""


def _uuid(raw: dict, key: str, *, required: bool = True) -> Optional[uuid.UUID]:
    value = raw.get(key)
    if value is None:
        if required:
            raise BadEvent(f"{key} is missing")
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise BadEvent(f"{key} is not a valid id")


def _time(raw: dict, key: str, *, required: bool) -> Optional[datetime]:
    value = raw.get(key)
    if value is None:
        if required:
            raise BadEvent(f"{key} is missing")
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise BadEvent(f"{key} is not a valid time")
    if parsed.tzinfo is None:
        raise BadEvent(f"{key} has no time zone")
    return parsed


def parse_event(raw: Any) -> dict:
    """Validate one replicated event and return the values to insert. Raises BadEvent."""
    if not isinstance(raw, dict):
        raise BadEvent("an event must be an object")
    activity, kind, venue = raw.get("activity"), raw.get("kind"), raw.get("venue_id")
    if activity not in ACTIVITY_OWNER:
        raise BadEvent("unknown activity")
    if kind not in KINDS:
        raise BadEvent("unknown kind")
    if venue not in VENUES:
        raise BadEvent("unknown venue")
    if ACTIVITY_OWNER[activity] != venue:  # single writer (golden rule 4), enforced on arrival as well
        raise BadEvent(f"{activity} is not recorded at the {venue} venue")
    seq = raw.get("venue_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise BadEvent("venue_seq must be a positive whole number")
    cycle = raw.get("completion_cycle", 1)
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise BadEvent("completion_cycle must be a positive whole number")
    flags = raw.get("flags") or []
    if not isinstance(flags, list) or not set(flags) <= FLAGS:
        raise BadEvent("flags are not valid")
    details = raw.get("details") or {}
    if not isinstance(details, dict):
        raise BadEvent("details must be an object")
    station = raw.get("station_id")
    if station is not None and not isinstance(station, str):
        raise BadEvent("station_id must be text")
    return {
        "event_id": _uuid(raw, "event_id"), "student_id": _uuid(raw, "student_id"), "activity": activity, "kind": kind,
        "venue_id": venue, "venue_seq": seq, "station_id": station, "operator_id": _uuid(raw, "operator_id"),
        "server_time": _time(raw, "server_time", required=True), "local_time": _time(raw, "local_time", required=False),
        "flags": list(flags), "details": json.dumps(details, default=str), "completion_cycle": cycle,
        "corrects_event_id": _uuid(raw, "corrects_event_id", required=False),
    }


# ------------------------------------------------------------------ one event
def _existing_conflicting(conn: Connection, ev: dict, constraint: Optional[str]):
    if constraint == "activity_events_venue_seq_key":
        return conn.execute(text("SELECT event_id FROM activity_events WHERE venue_id = :v AND venue_seq = :s"),
                            {"v": ev["venue_id"], "s": ev["venue_seq"]}).scalar()
    kinds = ("REVERSAL",) if constraint == "activity_events_one_reversal" else ("COMPLETE", "WAIVER")
    return conn.execute(
        text("SELECT event_id FROM activity_events WHERE student_id = :s AND activity = :a AND completion_cycle = :c "
             "AND kind = ANY (CAST(:k AS text[])) LIMIT 1"),
        {"s": ev["student_id"], "a": ev["activity"], "c": ev["completion_cycle"], "k": list(kinds)}).scalar()


def _classify_existing(conn: Connection, ev: dict) -> tuple[str, str, Any]:
    """The event_id is already stored: the same event again (DUPLICATE), or a different event under that id (CONFLICT)."""
    row = conn.execute(
        text("SELECT student_id, activity, kind, venue_id, venue_seq, completion_cycle, corrects_event_id "
             "FROM activity_events WHERE event_id = :e"), {"e": ev["event_id"]}).mappings().one()
    same = all(row[k] == ev[k] for k in ("student_id", "activity", "kind", "venue_id", "venue_seq", "completion_cycle",
                                          "corrects_event_id"))
    if same:
        return DUPLICATE, "", None
    return CONFLICT, "an event with this id is already stored with different content", ev["event_id"]


def _try_insert(conn: Connection, ev: dict, log_arrivals: bool) -> tuple[str, str, Any]:
    """Insert one event in its own savepoint. -> (status, reason, existing_event_id)."""
    try:
        with conn.begin_nested():
            row = conn.execute(_INSERT, ev).first()
            if row is None:
                return _classify_existing(conn, ev)
            if log_arrivals:  # numbered in COMMIT order by the locked counter (migration 0008)
                conn.execute(text("INSERT INTO sync_log (central_seq, event_id, venue_id) "
                                  "VALUES (next_counter('central_seq'), :e, :v)"), {"e": ev["event_id"], "v": ev["venue_id"]})
        return ACCEPTED, "", None
    except IntegrityError as exc:
        code = getattr(exc.orig, "pgcode", None)
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        message = str(exc.orig).splitlines()[0][:200] if exc.orig else "database rule"
        if code == "23505":
            reasons = {
                "activity_events_one_completion": "a second, different completion of the same activity for this student",
                "activity_events_one_reversal": "a second reversal of the same completion",
                "activity_events_venue_seq_key": "this venue number is already used by a different event",
            }
            if name in reasons:
                return CONFLICT, reasons[name], _existing_conflicting(conn, ev, name)
            return REJECTED, message, None
        if code == "23503":  # the student is not on this server yet (master data not loaded): try again later
            return RETRY, "the student is not on this server yet", None
        if code == "23514" and name is None:  # a Phase 2 trigger: the event it depends on has not arrived
            return PARKED, message, None
        return REJECTED, message, None


def _store_conflict(conn: Connection, ev: dict, raw: dict, existing: Any, reason: str, source: str) -> None:
    """Keep the incoming event in FULL and tell the Admin. Never merged, never dropped."""
    conn.execute(
        text("INSERT INTO conflict_events (event_id, venue_id, student_id, activity, kind, venue_seq, payload, "
             "existing_event_id, reason, source) VALUES (:e, :v, :s, :a, :k, :q, CAST(:p AS jsonb), :x, :r, :src) "
             "ON CONFLICT (event_id) DO NOTHING"),
        {"e": ev["event_id"], "v": ev["venue_id"], "s": ev["student_id"], "a": ev["activity"], "k": ev["kind"],
         "q": ev["venue_seq"], "p": json.dumps(raw, default=str), "x": existing, "r": reason, "src": source})
    known_student = conn.execute(text("SELECT EXISTS (SELECT 1 FROM students WHERE id = :s)"), {"s": ev["student_id"]}).scalar()
    raised = open_exception(
        conn, "CONFLICT", student_id=ev["student_id"] if known_student else None, venue_id=ev["venue_id"],
        event_id=ev["event_id"], details={"reason": reason, "activity": ev["activity"], "venue_seq": ev["venue_seq"],
                                          "existing_event_id": str(existing) if existing else None, "source": source})
    if raised:
        write_audit(conn, "SYNC_CONFLICT", venue_id=ev["venue_id"], reason=reason, student_id=ev["student_id"] if known_student else None,
                    activity=ev["activity"], event_id=ev["event_id"], details={"existing_event_id": str(existing) if existing else None})
        logger.warning("sync conflict stored: event=%s venue=%s %s", ev["event_id"], ev["venue_id"], reason)


def _park(conn: Connection, ev: dict, raw: dict, reason: str, source: str) -> None:
    conn.execute(
        text("INSERT INTO sync_parked (event_id, venue_id, venue_seq, payload, reason, source) "
             "VALUES (:e, :v, :q, CAST(:p AS jsonb), :r, :s) "
             "ON CONFLICT (event_id) DO UPDATE SET attempts = sync_parked.attempts + 1, reason = EXCLUDED.reason"),
        {"e": ev["event_id"], "v": ev["venue_id"], "q": ev["venue_seq"], "p": json.dumps(raw, default=str), "r": reason, "s": source})


def _unpark(conn: Connection, event_id) -> None:
    conn.execute(text("DELETE FROM sync_parked WHERE event_id = :e"), {"e": event_id})


def _drain_parked(conn: Connection, log_arrivals: bool, source: str) -> int:
    """Insert every parked event whose dependency has now arrived. Repeats until nothing more moves."""
    moved, progress = 0, True
    while progress:
        progress = False
        for row in conn.execute(text("SELECT event_id, payload FROM sync_parked ORDER BY venue_id, venue_seq")).mappings().all():
            try:
                ev = parse_event(row["payload"])
            except BadEvent:
                continue
            status, reason, existing = _try_insert(conn, ev, log_arrivals)
            if status in (ACCEPTED, DUPLICATE):
                _unpark(conn, row["event_id"])
                moved += 1
                progress = progress or status == ACCEPTED
            elif status == CONFLICT:
                _store_conflict(conn, ev, row["payload"], existing, reason, source)
                _unpark(conn, row["event_id"])
    return moved


# ------------------------------------------------------------------ a batch
def ingest_events(conn: Connection, raw_events: Iterable[Any], *, expected_venue: Optional[str] = None,
                  skip_venue: Optional[str] = None, log_arrivals: bool = False, source: str = "") -> list[IngestResult]:
    """Ingest a batch inside the caller's transaction. One result per input event, in input order.

    expected_venue  central: the venue whose key signed the request; another venue's event is REJECTED.
    skip_venue      a venue: its own events are never taken back in (IGNORED).
    log_arrivals    central: number every stored event in sync_log for the venues' pull cursors.
    """
    raws = list(raw_events)
    results: list[Optional[IngestResult]] = [None] * len(raws)
    todo: list[tuple[int, dict, dict]] = []
    for i, raw in enumerate(raws):
        try:
            ev = parse_event(raw)
        except BadEvent as exc:
            results[i] = IngestResult(str(raw.get("event_id")) if isinstance(raw, dict) else None, REJECTED, str(exc))
            continue
        if expected_venue is not None and ev["venue_id"] != expected_venue:
            results[i] = IngestResult(str(ev["event_id"]), REJECTED, f"this key may only send {expected_venue} events")
            continue
        if skip_venue is not None and ev["venue_id"] == skip_venue:
            results[i] = IngestResult(str(ev["event_id"]), IGNORED, "this venue's own event")
            continue
        todo.append((i, ev, raw))

    # Author order first (a venue's own numbering is dependency-valid); the parking table handles the rest.
    accepted = 0
    for i, ev, raw in sorted(todo, key=lambda t: (t[1]["venue_id"], t[1]["venue_seq"])):
        status, reason, existing = _try_insert(conn, ev, log_arrivals)
        if status == CONFLICT:
            _store_conflict(conn, ev, raw, existing, reason, source)
        elif status == PARKED:
            _park(conn, ev, raw, reason, source)
        elif status in (ACCEPTED, DUPLICATE):
            _unpark(conn, ev["event_id"])
            accepted += status == ACCEPTED
        results[i] = IngestResult(str(ev["event_id"]), status, reason)
    if accepted:
        _drain_parked(conn, log_arrivals, source)
    return [r for r in results if r is not None]

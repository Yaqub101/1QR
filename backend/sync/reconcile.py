"""Reconciliation (SYSTEM_SPEC 11.7). Runs on central and on every venue after each sync, and on demand.

Idempotent: running it twice in a row changes nothing the second time.

  PROVISIONAL events   An event accepted while the owning venue's data was stale (SYSTEM_SPEC 11.5) has an OPEN
                       PROVISIONAL_UNCONFIRMED exception (raised in the same commit that accepted it). Here, for
                       every still-active provisional event: if every cross-venue prerequisite is now stored, the
                       exception is CLOSED BY THE SYSTEM ("arrived by sync"); if one is still missing the item stays
                       OPEN for the Admin. An event that arrived by sync already flagged PROVISIONAL, with no
                       exception at this server yet, gets one when its prerequisite is missing here.
                       An item an Admin resolved by hand is never reopened.
  venue_seq GAPS       Each venue numbers its events 1, 2, 3... with no holes (migration 0003). A missing number
                       below the highest stored one, or below the number a venue reported with a fully-drained
                       heartbeat, means an event went missing: one OPEN SEQ_GAP item per missing range, closed by
                       the system when the range fills in.
  CONFLICTs            Raised when the duplicate arrives (backend/sync/ingest.py), not here.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.engine.activities import ACTIVITY_CONFIGS
from backend.engine.queries import active_completion
from backend.security.ownership import VENUES
from backend.sync.exceptions_log import close_exception, open_exception

logger = logging.getLogger("backend.sync")

ARRIVED = "The missing record arrived by sync."
REVERSED = "The provisional record was reversed by an Admin, so there is nothing left to confirm."
GAP_FILLED = "Every missing record in this range has now arrived."


def _provisional_pass(conn: Connection) -> dict:
    opened = closed = 0
    events = conn.execute(text(
        "SELECT e.event_id, e.student_id, e.activity, e.venue_id, EXISTS (SELECT 1 FROM activity_events r WHERE r.kind = 'REVERSAL' "
        "AND r.student_id = e.student_id AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle) AS reversed "
        "FROM activity_events e WHERE e.kind = 'COMPLETE' AND 'PROVISIONAL' = ANY (e.flags) ORDER BY e.venue_seq")).mappings().all()
    for e in events:
        latest = conn.execute(text("SELECT id, status FROM exceptions WHERE type = 'PROVISIONAL_UNCONFIRMED' AND event_id = :e "
                                   "ORDER BY id DESC LIMIT 1"), {"e": e["event_id"]}).mappings().one_or_none()
        if e["reversed"]:  # an Admin reversed the provisional record itself: there is nothing left to confirm
            if latest is not None and latest["status"] == "OPEN":
                closed += close_exception(conn, latest["id"], REVERSED)
            continue
        missing = [p.activity for p in ACTIVITY_CONFIGS[e["activity"]].cross_venue_prerequisites()
                   if active_completion(conn, e["student_id"], p.activity) is None]
        if not missing:
            if latest is not None and latest["status"] == "OPEN":
                closed += close_exception(conn, latest["id"], ARRIVED)
        elif latest is None:
            opened += open_exception(conn, "PROVISIONAL_UNCONFIRMED", student_id=e["student_id"], venue_id=e["venue_id"],
                                     event_id=e["event_id"], details={"activity": e["activity"], "missing": missing})
    return {"provisional_opened": opened, "provisional_closed": closed}


def _ranges(numbers: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in numbers:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def missing_ranges(conn: Connection, venue: str) -> list[tuple[int, int]]:
    """Missing venue_seq ranges for `venue` at this server."""
    highest = conn.execute(text("SELECT coalesce(max(venue_seq), 0) FROM activity_events WHERE venue_id = :v"), {"v": venue}).scalar_one()
    reported = conn.execute(text("SELECT reported_seq FROM sync_state WHERE peer = :v AND coalesce(pending_reported, 1) = 0"),
                            {"v": venue}).scalar()
    upper = max(int(highest), int(reported or 0))  # the tail counts only when the venue said its outbox was empty
    if upper == 0:
        return []
    numbers = [r[0] for r in conn.execute(
        text("SELECT g FROM generate_series(1, :u) g WHERE NOT EXISTS "
             "(SELECT 1 FROM activity_events e WHERE e.venue_id = :v AND e.venue_seq = g) ORDER BY g"), {"u": upper, "v": venue})]
    return _ranges(numbers)


def _gap_pass(conn: Connection) -> dict:
    opened = closed = 0
    for venue in VENUES:
        missing = missing_ranges(conn, venue)
        open_items = conn.execute(text(
            "SELECT id, CAST(details->>'first_missing' AS bigint) AS a, CAST(details->>'last_missing' AS bigint) AS b "
            "FROM exceptions WHERE type = 'SEQ_GAP' AND status = 'OPEN' AND venue_id = :v"), {"v": venue}).mappings().all()
        for item in open_items:
            present = conn.execute(text("SELECT count(*) FROM activity_events WHERE venue_id = :v AND venue_seq BETWEEN :a AND :b"),
                                   {"v": venue, "a": item["a"], "b": item["b"]}).scalar_one()
            if present == item["b"] - item["a"] + 1:
                closed += close_exception(conn, item["id"], GAP_FILLED)
        for first, last in missing:
            if conn.execute(text("SELECT EXISTS (SELECT 1 FROM exceptions WHERE type = 'SEQ_GAP' AND status = 'OPEN' AND venue_id = :v "
                                 "AND CAST(details->>'first_missing' AS bigint) <= :a AND CAST(details->>'last_missing' AS bigint) >= :b)"),
                            {"v": venue, "a": first, "b": last}).scalar():
                continue  # already covered by an open item (a range that has only partly filled in)
            opened += open_exception(conn, "SEQ_GAP", venue_id=venue,
                                     details={"first_missing": first, "last_missing": last, "count": last - first + 1})
            logger.warning("venue_seq gap: venue=%s missing %s-%s", venue, first, last)
    return {"gaps_opened": opened, "gaps_closed": closed}


def reconcile_conn(conn: Connection) -> dict:
    summary = {}
    summary.update(_provisional_pass(conn))
    summary.update(_gap_pass(conn))
    return summary


def reconcile(engine) -> dict:
    with engine.begin() as conn:
        return reconcile_conn(conn)

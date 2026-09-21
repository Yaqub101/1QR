"""Central's side of sync: accept a venue's pushed events, and serve the other venues' events by cursor.

    push   A venue sends a batch (possibly EMPTY: that is its heartbeat) signed by its own key. Every event is
           ingested idempotently; the response lists a status per event and is sent only AFTER the transaction
           committed, so "ACCEPTED / DUPLICATE" always means "durably stored here". The venue marks an outbox row
           sent only on that answer.
    pull   A venue asks for events after its cursor. The cursor is `sync_log.central_seq`, numbered in COMMIT
           order under a lock (migration 0008), so a late-committing transaction can never land behind a cursor
           that has already moved on. The reply also says how long ago each OTHER venue last reported in: that
           is how a venue knows whether a peer's data is fresh (backend/sync/status.py).

Central never originates an event (golden rule 4); it only stores what venues authored.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from backend.security.ownership import VENUES
from backend.sync import reconcile as reconcile_svc
from backend.sync.ingest import ingest_events

logger = logging.getLogger("backend.sync")

MAX_PUSH_EVENTS = 1000
MAX_PULL_EVENTS = 1000


def current_epoch(conn) -> str:
    return conn.execute(text("SELECT value FROM sync_meta WHERE key = 'epoch'")).scalar_one()


def push(engine, venue: str, *, events: list, pending_after: int = 0, last_seq: Optional[int] = None,
         drain_total: int = 0, drain_done: int = 0) -> dict:
    if len(events) > MAX_PUSH_EVENTS:
        raise ValueError("too many events in one request")
    with engine.begin() as conn:
        results = ingest_events(conn, events, expected_venue=venue, log_arrivals=True, source=f"push:{venue}")
        conn.execute(
            text("INSERT INTO sync_state (peer, last_success_at, last_push_at, pending_reported, reported_seq, drain_total, drain_done) "
                 "VALUES (:v, now(), now(), :p, :s, :t, :d) "
                 "ON CONFLICT (peer) DO UPDATE SET last_success_at = now(), last_push_at = now(), pending_reported = :p, "
                 "reported_seq = :s, drain_total = :t, drain_done = :d, last_error = NULL"),
            {"v": venue, "p": max(int(pending_after), 0), "s": last_seq, "t": max(int(drain_total), 0), "d": max(int(drain_done), 0)})
        epoch = current_epoch(conn)
    # The answer is built only after COMMIT. Reconciliation is separate and best-effort: a fault there must never
    # stop events flowing.
    try:
        reconcile_svc.reconcile(engine)
    except Exception:
        logger.exception("reconciliation after a push failed (sync itself is unaffected)")
    return {"results": [r.to_dict() for r in results], "epoch": epoch}


def pull(engine, venue: str, *, after: int, limit: int) -> dict:
    limit = max(1, min(int(limit), MAX_PULL_EVENTS))
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT l.central_seq, l.venue_id, to_jsonb(e) AS event FROM sync_log l "
                 "JOIN activity_events e ON e.event_id = l.event_id WHERE l.central_seq > :a ORDER BY l.central_seq LIMIT :n"),
            {"a": max(int(after), 0), "n": limit}).mappings().all()
        ages = {r["peer"]: r["age"] for r in conn.execute(text(
            "SELECT peer, extract(epoch FROM now() - last_success_at) AS age FROM sync_state WHERE peer <> 'central'")).mappings()}
        epoch = current_epoch(conn)
    return {
        "events": [{"central_seq": r["central_seq"], "event": r["event"]} for r in rows if r["venue_id"] != venue],
        # The cursor moves past the venue's OWN events too (they are not sent back), so a page of nothing but its
        # own events still makes progress.
        "next_cursor": rows[-1]["central_seq"] if rows else max(int(after), 0),
        "has_more": len(rows) == limit,
        "epoch": epoch,
        "peers": {v: (float(ages[v]) if ages.get(v) is not None else None) for v in VENUES if v != venue},
    }

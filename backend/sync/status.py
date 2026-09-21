"""Sync status (the 🟢 / 🟡 / 🔵 indicator) and peer FRESHNESS (SYSTEM_SPEC 9, 11.5).

Two different questions live here, and they must not be confused:

  Is this server connected?   The traffic-light. Derived from the last successful exchange with central.
  Is a peer venue's data
  current HERE?               Freshness. `sync_state.data_as_of` for that peer. It is set only when a pull
                              reaches the END of central's log (nothing left to fetch) AND central says that
                              peer last reported in `age` seconds ago: data_as_of = now - age. A pull that
                              succeeds while the peer itself is offline therefore does NOT make the peer look
                              fresh. The cross-venue rule (backend/engine/cross_venue.py) reads only this.

All times are the database server's clock (never a laptop's).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.security.ownership import VENUES

ONLINE, OFFLINE, SYNCING = "ONLINE", "OFFLINE", "SYNCING"
EMOJI = {ONLINE: "🟢", OFFLINE: "🟡", SYNCING: "🔵"}
DEFAULT_WINDOW_SECONDS = 120


def freshness_window(conn: Connection) -> int:
    """Seconds a peer's data counts as fresh. Set in `settings` (default 2 minutes); 10 s to 1 hour."""
    value = conn.execute(text("SELECT freshness_window_seconds FROM settings WHERE id = 1")).scalar()
    return int(value) if value else DEFAULT_WINDOW_SECONDS


def peer_age_seconds(conn: Connection, peer: str) -> Optional[float]:
    """How old the freshest knowledge of `peer` is at this server, or None if it has never been known."""
    return conn.execute(
        text("SELECT extract(epoch FROM now() - data_as_of) FROM sync_state WHERE peer = :p AND data_as_of IS NOT NULL"),
        {"p": peer}).scalar()


def peer_is_fresh(conn: Connection, peer: str) -> bool:
    """THE freshness test: was `peer`'s data known to be current here within the window? Never synced = stale."""
    age = peer_age_seconds(conn, peer)
    return age is not None and float(age) <= freshness_window(conn)


def classify(age: Optional[float], pending: Optional[int], done: int, total: int, window: int,
             never: str = "LOCAL — sync has not started") -> dict:
    """Turn 'how long since the last good exchange, how much is waiting' into the traffic light."""
    if age is None:
        return {"state": OFFLINE, "emoji": EMOJI[OFFLINE], "label": never}
    waiting = "?" if pending is None else pending
    if float(age) > window:
        return {"state": OFFLINE, "emoji": EMOJI[OFFLINE], "label": f"OFFLINE — LOCAL MODE · {waiting} waiting"}
    if pending:
        whole = max(total, done + pending)
        return {"state": SYNCING, "emoji": EMOJI[SYNCING], "label": f"SYNCING {done} / {whole}"}
    return {"state": ONLINE, "emoji": EMOJI[ONLINE], "label": "ONLINE — all synced"}


def this_server(conn: Connection, settings) -> dict:
    """How THIS server sees its own link to central (a venue's view of itself)."""
    window = freshness_window(conn)
    row = conn.execute(text(
        "SELECT extract(epoch FROM now() - greatest(last_push_at, last_success_at)) AS age, drain_done, drain_total, "
        "last_error, epoch FROM sync_state WHERE peer = 'central'")).mappings().one_or_none()
    pending = int(conn.execute(text("SELECT count(*) FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL")).scalar_one())
    if row is None:
        return {**classify(None, pending, 0, 0, window), "pending": pending, "last_error": None}
    status = classify(row["age"], pending, row["drain_done"], row["drain_total"], window)
    return {**status, "pending": pending, "last_error": row["last_error"]}


def peers(conn: Connection) -> list[dict]:
    """Per peer venue: when its data was last known current here, how old that is, and whether that is fresh."""
    window = freshness_window(conn)
    known = {r["peer"]: r for r in conn.execute(text(
        "SELECT peer, data_as_of, last_success_at, extract(epoch FROM now() - data_as_of) AS age "
        "FROM sync_state WHERE peer <> 'central'")).mappings()}
    out = []
    for venue in VENUES:
        r = known.get(venue)
        age = float(r["age"]) if r and r["age"] is not None else None
        out.append({"venue": venue, "data_as_of": r["data_as_of"] if r else None, "age_seconds": age,
                    "last_pull_at": r["last_success_at"] if r else None, "fresh": age is not None and age <= window})
    return out


def venues_seen_by_central(conn: Connection) -> list[dict]:
    """Central's view: one traffic light per venue, from the heartbeat each venue sends with every push."""
    window = freshness_window(conn)
    rows = {r["peer"]: r for r in conn.execute(text(
        "SELECT peer, extract(epoch FROM now() - last_success_at) AS age, last_success_at, pending_reported, drain_done, "
        "drain_total, reported_seq FROM sync_state WHERE peer <> 'central'")).mappings()}
    out = []
    for venue in VENUES:
        r = rows.get(venue)
        if r is None:
            status = classify(None, None, 0, 0, window, never="OFFLINE — never connected")
            out.append({"venue": venue, **status, "pending": None, "last_sync_at": None})
        else:
            status = classify(r["age"], r["pending_reported"], r["drain_done"], r["drain_total"], window)
            out.append({"venue": venue, **status, "pending": r["pending_reported"], "last_sync_at": r["last_success_at"]})
    return out

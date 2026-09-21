"""The venue's background sync worker (SYSTEM_SPEC 9): push the outbox, pull the other venues, forever.

The rules that make it safe:

  PUSH   Send unsent outbox rows in batches, oldest first. An outbox row is marked SENT only when central's
         answer names that event with ACCEPTED / DUPLICATE / PARKED / CONFLICT, i.e. central has committed it.
         If the request fails, if central's answer is incomplete, or if THIS process dies between central's
         commit and our mark, the row stays unsent and is simply sent again next time; central treats the
         repeat as DUPLICATE. So a crash loses nothing and duplicates nothing. An empty push still goes out: it
         is the heartbeat central's dashboard and every peer's freshness clock depend on.
  PULL   Ask central for events after our cursor, apply them (idempotent, read-only history) and move the cursor
         in the SAME transaction. When the pull reaches the end of central's log it records, per peer venue,
         `data_as_of = now - age`, where age is how long ago that peer last reported to central.
  RETRY  Any failure: keep everything, back off (base * 2^n, capped, with jitter), try again. Operators never
         see any of this: the worker never touches the scan path.
  EPOCH  If central's epoch changes (central was rebuilt) the cursor restarts at zero and everything is
         re-pulled; the events already held are DUPLICATEs, so nothing is applied twice.

`_mark` is a module-level function on purpose: tests replace it to simulate a crash after central committed.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.sync import ingest, reconcile as reconcile_svc
from backend.sync.client import SyncAuthError, SyncClient, SyncConfigError, SyncError, SyncUnavailable
from backend.sync.exceptions_log import open_exception

logger = logging.getLogger("backend.sync")

MAX_BATCHES_PER_CYCLE = 50


@dataclass
class CycleReport:
    ok: bool = True
    pushed: int = 0          # events central acknowledged this cycle
    heartbeat: bool = False  # an empty push went through
    pulled: int = 0          # foreign events applied (new)
    errors: list = field(default_factory=list)


def _mark(conn: Connection, rows: list, results: list, max_attempts: int) -> int:
    """Apply central's per-event answers to the outbox. Returns how many rows are now SENT."""
    answers = {r.get("event_id"): r for r in results if isinstance(r, dict)}
    sent = 0
    for row in rows:
        answer = answers.get(str(row["event_id"]))
        if answer is None:
            continue  # central said nothing about it: leave it unsent
        status = answer.get("status")
        if status in ingest.ACKNOWLEDGED:
            sent += conn.execute(text("UPDATE outbox SET sent_at = now(), last_error = NULL WHERE id = :i AND sent_at IS NULL"),
                                 {"i": row["id"]}).rowcount
        elif status == ingest.REJECTED or (status == ingest.RETRY and row["attempts"] + 1 >= max_attempts):
            reason = (answer.get("reason") or "refused by central")[:300]
            conn.execute(text("UPDATE outbox SET rejected_at = now(), last_error = :r, attempts = attempts + 1 "
                              "WHERE id = :i AND sent_at IS NULL"), {"i": row["id"], "r": reason})
            open_exception(conn, "SYNC_REJECTED", venue_id=None, event_id=row["event_id"], details={"reason": reason})
            logger.error("central refused event %s for good: %s", row["event_id"], reason)
        elif status == ingest.RETRY:
            conn.execute(text("UPDATE outbox SET attempts = attempts + 1, last_error = :r WHERE id = :i AND sent_at IS NULL"),
                         {"i": row["id"], "r": (answer.get("reason") or "")[:300]})
    return sent


class SyncWorker:
    def __init__(self, engine, settings, client: Optional[SyncClient] = None, *, rng=random.random):
        self.engine, self.settings, self.venue = engine, settings, settings.venue_id
        self.client = client or SyncClient(
            settings.central_url, settings.venue_api_key, timeout=settings.sync_timeout_seconds,
            require_tls=settings.sync_require_tls, ca_file=settings.central_ca_file)
        self.rng = rng
        self.failures = 0

    # ------------------------------------------------------------------ bookkeeping
    def _record_error(self, kind: str, exc: Exception) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO sync_state (peer, last_error, last_error_at) VALUES ('central', :e, now()) "
                              "ON CONFLICT (peer) DO UPDATE SET last_error = :e, last_error_at = now()"),
                         {"e": f"{kind}: {exc}"[:500]})

    def _clear_error(self, kind: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE sync_state SET last_error = NULL WHERE peer = 'central' AND last_error LIKE :p"),
                         {"p": f"{kind}:%"})

    def backoff_delay(self) -> float:
        """Seconds to wait after `failures` failed cycles in a row: base * 2^(n-1), capped, with jitter."""
        s = self.settings
        raw = min(s.sync_backoff_max_seconds, s.sync_backoff_base_seconds * (2 ** max(self.failures - 1, 0)))
        return raw * (0.5 + 0.5 * self.rng())

    # ------------------------------------------------------------------ push
    def _next_batch(self) -> list:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(
                text("SELECT id, event_id, payload, attempts FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL "
                     "ORDER BY id LIMIT :n"), {"n": self.settings.sync_batch_size}).mappings()]

    def push_once(self) -> tuple[int, bool]:
        """Drain the outbox (up to a cap of batches). Returns (events acknowledged, heartbeat sent)."""
        with self.engine.connect() as conn:
            pending = int(conn.execute(text("SELECT count(*) FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL")).scalar_one())
            last_seq = int(conn.execute(text("SELECT coalesce(max(venue_seq), 0) FROM activity_events WHERE venue_id = :v"),
                                        {"v": self.venue}).scalar_one())
            state = conn.execute(text("SELECT drain_total, drain_done FROM sync_state WHERE peer = 'central'")).mappings().one_or_none()
        total, done = (state["drain_total"], state["drain_done"]) if state and pending and state["drain_total"] else (pending, 0)
        acknowledged, heartbeat = 0, False
        for _ in range(MAX_BATCHES_PER_CYCLE):
            rows = self._next_batch()
            remaining_after = max(pending - len(rows), 0)
            total = max(total, done + len(rows) + remaining_after) if pending else 0
            response = self.client.push([r["payload"] for r in rows], pending_after=remaining_after, last_seq=last_seq,
                                        drain_total=total, drain_done=done + len(rows) if pending else 0)
            results = response.get("results")
            if not isinstance(results, list):
                raise SyncUnavailable("central's answer had no results")
            with self.engine.begin() as conn:  # central has COMMITTED; only now is anything marked sent
                sent = _mark(conn, rows, results, self.settings.sync_max_attempts)
                pending = int(conn.execute(text("SELECT count(*) FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL")).scalar_one())
                done = done + sent if pending else 0
                total = total if pending else 0
                conn.execute(text("INSERT INTO sync_state (peer, last_push_at, drain_total, drain_done) VALUES ('central', now(), :t, :d) "
                                  "ON CONFLICT (peer) DO UPDATE SET last_push_at = now(), drain_total = :t, drain_done = :d"),
                             {"t": total, "d": done})
            acknowledged += sent
            heartbeat = True
            if not rows or pending == 0:
                break
            if sent == 0:  # central would not take anything this round: stop rather than spin
                break
        return acknowledged, heartbeat

    # ------------------------------------------------------------------ pull
    def pull_once(self) -> int:
        """Apply the other venues' events. Returns how many were NEW here."""
        with self.engine.connect() as conn:
            state = conn.execute(text("SELECT cursor, epoch FROM sync_state WHERE peer = 'central'")).mappings().one_or_none()
        cursor, epoch = (state["cursor"], state["epoch"]) if state else (0, None)
        applied = 0
        for _ in range(MAX_BATCHES_PER_CYCLE):
            reply = self.client.pull(after=cursor, limit=self.settings.sync_batch_size)
            if epoch is not None and reply.get("epoch") != epoch and cursor != 0:
                logger.warning("central's epoch changed (it was rebuilt): re-pulling from the start")
                cursor, epoch = 0, reply.get("epoch")
                with self.engine.begin() as conn:
                    conn.execute(text("UPDATE sync_state SET cursor = 0, epoch = :e WHERE peer = 'central'"), {"e": epoch})
                continue
            items = reply.get("events")
            if not isinstance(items, list) or "next_cursor" not in reply:
                raise SyncUnavailable("central's answer was not a pull page")
            with self.engine.begin() as conn:
                results = ingest.ingest_events(conn, [i["event"] for i in items], skip_venue=self.venue, source="pull")
                next_cursor = int(reply["next_cursor"])
                stalled = next((n for n, r in enumerate(results) if r.status == ingest.RETRY), None)
                if stalled is not None:  # do not move past an event we could not store yet (master data not loaded)
                    next_cursor = int(items[stalled]["central_seq"]) - 1
                for r in results:
                    if r.status == ingest.REJECTED:
                        open_exception(conn, "SYNC_REJECTED", event_id=r.event_id, details={"reason": r.reason, "direction": "pull"})
                applied += sum(1 for r in results if r.status == ingest.ACCEPTED)
                caught_up = not reply.get("has_more") and stalled is None
                conn.execute(text("INSERT INTO sync_state (peer, cursor, epoch, last_success_at) VALUES ('central', :c, :e, now()) "
                                  "ON CONFLICT (peer) DO UPDATE SET cursor = :c, epoch = :e, last_success_at = now()"),
                             {"c": next_cursor, "e": reply.get("epoch")})
                if caught_up:  # everything central holds is applied here: each peer is as current as central's last word on it
                    for peer, age in (reply.get("peers") or {}).items():
                        if age is not None:
                            conn.execute(
                                text("INSERT INTO sync_state (peer, data_as_of, last_success_at) "
                                     "VALUES (:p, now() - make_interval(secs => :a), now()) "
                                     "ON CONFLICT (peer) DO UPDATE SET data_as_of = now() - make_interval(secs => :a), last_success_at = now()"),
                                {"p": peer, "a": float(age)})
                        else:
                            conn.execute(text("INSERT INTO sync_state (peer, last_success_at) VALUES (:p, now()) "
                                              "ON CONFLICT (peer) DO UPDATE SET last_success_at = now()"), {"p": peer})
            cursor, epoch = next_cursor, reply.get("epoch")
            if stalled is not None:
                raise SyncUnavailable("an event is waiting for its student to be loaded on this server")
            if not reply.get("has_more"):
                break
        return applied

    # ------------------------------------------------------------------ one cycle, and forever
    def cycle(self) -> CycleReport:
        report = CycleReport()
        for kind, step in (("push", self._push_step), ("pull", self._pull_step)):
            if not report.ok and any(e.startswith("push:") for e in report.errors):
                break  # push could not reach central (or was refused): pulling from the same place would fail the same way
            try:
                step(report)
                self._clear_error(kind)
            except SyncAuthError as exc:
                report.ok = False
                report.errors.append(f"{kind}: {exc}")
                logger.error("sync %s refused: %s (the venue's API key needs attention)", kind, exc)
                self._record_error(kind, exc)
            except SyncConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means: keep everything, back off, retry
                report.ok = False
                report.errors.append(f"{kind}: {exc}")
                logger.warning("sync %s failed (will retry): %s", kind, exc)
                try:
                    self._record_error(kind, exc)
                except Exception:  # the database itself is unwell; the outbox is untouched either way
                    logger.exception("could not record the sync error")
        if report.ok:
            self.failures = 0
            try:
                reconcile_svc.reconcile(self.engine)
            except Exception:
                logger.exception("reconciliation after sync failed (sync itself is fine)")
        else:
            self.failures += 1
        return report

    def _push_step(self, report: CycleReport) -> None:
        report.pushed, report.heartbeat = self.push_once()

    def _pull_step(self, report: CycleReport) -> None:
        report.pulled = self.pull_once()

    def run_forever(self, stop: threading.Event) -> None:
        logger.info("sync worker started: venue=%s central=%s", self.venue, self.settings.central_url)
        while not stop.is_set():
            try:
                ok = self.cycle().ok
            except SyncConfigError as exc:
                logger.error("sync is not configured: %s", exc)
                return
            stop.wait(self.settings.sync_interval_seconds if ok else self.backoff_delay())
        logger.info("sync worker stopped")


class WorkerThread:
    """Runs a SyncWorker on a daemon thread for the life of the app."""

    def __init__(self, worker: SyncWorker):
        self.worker, self.stop_event = worker, threading.Event()
        self.thread = threading.Thread(target=worker.run_forever, args=(self.stop_event,), name="sync-worker", daemon=True)

    def start(self) -> "WorkerThread":
        self.thread.start()
        return self

    def stop(self, timeout: float = 15.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout)

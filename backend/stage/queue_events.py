"""backend/stage/queue_events.py — Realtime SSE queue broadcast with PostgreSQL LISTEN/NOTIFY.

Requirement 2 & 4:
- Endpoint: /events/queue
- Broadcasts on every queue change (student queued, called, staged) to all subscribers
- Multi-worker / multi-instance safe via PostgreSQL LISTEN/NOTIFY on channel 'queue_events'
- Periodic ping heartbeat (2s) to prevent idle timeouts on Render & proxies
- Fallback for non-Postgres test environments
"""
from __future__ import annotations

import json
import logging
import select
import time
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def sse_message(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def iter_queue_events(
    engine: Engine,
    *,
    heartbeat_seconds: float = 2.0,
    poll_seconds: float = 0.5,
    stop=lambda: False,
) -> Iterator[str]:
    """SSE generator yielding events for queue changes across any number of workers.
    
    Emits:
    - 'sync' upon initial connection
    - 'queue' whenever a student is queued, called, or staged
    - 'ping' heartbeats every 2.0 seconds while idle
    """
    yield sse_message("sync", {"time": time.time()})

    is_postgres = engine.dialect.name == "postgresql"
    if is_postgres:
        raw_conn = None
        try:
            import psycopg2.extensions
            raw_conn = engine.raw_connection()
            dbapi_conn = getattr(raw_conn, "connection", raw_conn)
            # Autocommit is required for LISTEN / NOTIFY in psycopg2
            dbapi_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            curs = dbapi_conn.cursor()
            curs.execute("LISTEN queue_events;")

            while not stop():
                readable, _, _ = select.select([dbapi_conn], [], [], heartbeat_seconds)
                if readable:
                    dbapi_conn.poll()
                    while dbapi_conn.notifies:
                        notify = dbapi_conn.notifies.pop(0)
                        try:
                            payload = json.loads(notify.payload)
                        except Exception:
                            payload = {"raw": notify.payload}
                        yield sse_message("queue", payload)
                else:
                    yield sse_message("ping", {})
        except GeneratorExit:
            pass
        except Exception as exc:
            logger.warning("PostgreSQL LISTEN queue_events interrupted: %s; falling back", exc)
        finally:
            if raw_conn is not None:
                try:
                    curs.close()
                except Exception:
                    pass
                try:
                    raw_conn.close()
                except Exception:
                    pass
            return

    # Fallback polling for SQLite / in-memory tests
    last_version = None
    last_ping = time.time()
    while not stop():
        now = time.time()
        try:
            with engine.connect() as conn:
                v = conn.execute(
                    text("SELECT count(*)::text || ':' || coalesce(max(queue_position), 0)::text FROM queue")
                ).scalar()
            if v != last_version:
                yield sse_message("queue", {"action": "poll", "version": v})
                last_version = v
                last_ping = now
            elif now - last_ping >= heartbeat_seconds:
                yield sse_message("ping", {})
                last_ping = now
        except GeneratorExit:
            break
        except Exception:
            pass
        time.sleep(poll_seconds)

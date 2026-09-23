"""The PUBLIC LED payload and the server-sent-events streams (SYSTEM_SPEC 13, 18; golden rules 9 and 10).

THE LED PAYLOAD (this shape is a contract with static/led.js and the tests; do not add fields):

    {
      "mode":    "HOME" | "SHOWING",
      "version": <int>,                                  # bumped on every change; never an id
      "holding": {"title": <event name>, "text": <holding-screen text>},
      "student": null | {"name", "photo_url", "programme", "school", "award"},
      "preload": [{"photo_url"}, ...]                    # the next few photos only, so they are cached
    }

It is built ONLY from the approved display_snapshot row of the student the Stage Controller pointed the LED
at, plus event settings. It never reads the students table, so a PRN, phone, email, sequence number, seat or
internal id cannot reach it, and photo_url uses an opaque random key, not a student id.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.stage import state as stage_state

logger = logging.getLogger("backend.engine")
PRELOAD_COUNT = 5


def led_payload(conn: Connection) -> dict:
    st = stage_state.read_state(conn)
    settings = conn.execute(text("SELECT event_name, holding_screen_text FROM settings WHERE id = 1")).mappings().one()
    student = None
    if st["display_student_id"] is not None:
        row = conn.execute(
            text("SELECT display_name, programme, school, award, led_key FROM display_snapshot WHERE student_id = :s"),
            {"s": st["display_student_id"]},
        ).mappings().one_or_none()
        if row is not None:
            student = {"name": row["display_name"], "photo_url": f"/led/photo/{row['led_key']}",
                       "programme": row["programme"], "school": row["school"], "award": row["award"]}
    upcoming = conn.execute(
        text("SELECT d.led_key FROM queue q JOIN display_snapshot d ON d.student_id = q.student_id "
             "WHERE q.status = 'QUEUED' ORDER BY q.queue_position LIMIT :n"), {"n": PRELOAD_COUNT},
    ).scalars().all()
    return {
        "mode": "SHOWING" if student else "HOME",
        "version": st["version"],
        "holding": {"title": settings["event_name"], "text": settings["holding_screen_text"]},
        "student": student,
        "preload": [{"photo_url": f"/led/photo/{key}"} for key in upcoming],
    }


def caller_payload(conn: Connection) -> dict:
    """The internal Caller screen's payload (role/flow redesign Phase R3): the SAME LED payload, cut down to the
    name and the programme / degree. Built from led_payload() itself, so the two screens read one row of the
    approved display_snapshot through one function and can never disagree about who is on screen.

        {"mode": "HOME" | "SHOWING", "version": <int>, "student": null | {"name", "programme"}}
    """
    shown = led_payload(conn)
    student = shown["student"]
    return {
        "mode": shown["mode"],
        "version": shown["version"],
        "student": None if student is None else {"name": student["name"], "programme": student["programme"]},
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


# What each stream watches. The LED watches ONLY the stage state, so a queue scan can never push anything to
# the audience screen (golden rule 9). The Stage screen also watches the waiting queue, so its list of waiting
# students updates the moment the Queue operator adds someone.
LED_VERSION_SQL = "SELECT version::text FROM stage_state WHERE id = 1"
STAGE_VERSION_SQL = (
    "SELECT (SELECT version FROM stage_state WHERE id = 1)::text || ':' || count(*)::text || ':' "
    "|| coalesce(max(queue_position), 0)::text FROM queue WHERE status = 'QUEUED'"
)


def iter_events(engine, build_payload: Callable[[Connection], dict], *, poll_seconds: float = 0.25,
                heartbeat_seconds: float = 2.0, sleep: Callable = time.sleep,
                monotonic: Callable = time.monotonic, stop: Callable = lambda: False,
                version_sql: str = LED_VERSION_SQL) -> Iterator[str]:
    """Yield an SSE `state` event whenever the watched version changes (the first one immediately), and a
    `ping` heartbeat when quiet, so a page can tell "connected and idle" from "connection lost".

    Only the version is polled (one tiny query); the payload is built when it changes. A database hiccup
    never ends the stream: it stays silent, the page's own watchdog handles the gap, and it resumes.
    """
    last_version = None
    last_sent = monotonic()
    while not stop():
        out = None
        try:
            with engine.connect() as conn:  # the connection is released BEFORE anything is yielded
                version = conn.execute(text(version_sql)).scalar_one()
                now = monotonic()
                if version != last_version:
                    out = _sse("state", build_payload(conn))
                    last_version = version
                    last_sent = now
                elif now - last_sent >= heartbeat_seconds:
                    out = _sse("ping", {})
                    last_sent = now
        except Exception:  # noqa: BLE001 - the stream must survive a database blip
            logger.warning("event stream could not read the stage state; staying silent and retrying", exc_info=True)
        if out is not None:
            yield out  # never suspended while holding a database connection or transaction
        sleep(poll_seconds)


def iter_led_events(engine, settings, **kwargs) -> Iterator[str]:
    return iter_events(engine, led_payload, version_sql=LED_VERSION_SQL, **kwargs)


def iter_caller_events(engine, settings, **kwargs) -> Iterator[str]:
    """The Caller screen's stream: the LED's change signal exactly, so both screens change on the same poll."""
    return iter_events(engine, caller_payload, version_sql=LED_VERSION_SQL, **kwargs)


def iter_stage_events(engine, build_payload: Callable[[Connection], dict], **kwargs) -> Iterator[str]:
    """The Stage screen's private stream: also pushes when the waiting queue changes."""
    return iter_events(engine, build_payload, version_sql=STAGE_VERSION_SQL, **kwargs)

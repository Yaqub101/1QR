"""Helpers shared by the Admin test modules (Phases 13 and 16). Rows are inserted DIRECTLY as SQL, so the
expected answers in the tests never depend on the code under test.
"""
import csv
import io
import json
from types import SimpleNamespace

from sqlalchemy import text

from tests.test_auth import OWNER
from tests.test_station_engine import make_student


def add_student(engine, *, school, programme="B.Tech Computer Science", name=None):
    s = make_student(engine, name=name)
    with engine.begin() as c:
        c.execute(text("UPDATE students SET school = :sc, programme = :p WHERE id = :i"), {"sc": school, "p": programme, "i": s.id})
    s.school, s.programme = school, programme
    return s


def add_event(engine, student, activity, *, kind="COMPLETE", flags=(), details=None, cycle=1, corrects=None, hours_ago=0):
    """One event row, exactly as given. Returns its event_id."""
    if details is None:
        details = {} if kind == "COMPLETE" else {"reason": "test"}
    flags = list(flags) or (["CORRECTED"] if kind in ("WAIVER", "REVERSAL") else [])
    with engine.begin() as c:
        return c.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details, "
                 "completion_cycle, corrects_event_id, server_time) VALUES (:s, :a, :k, :v, :st, gen_random_uuid(), "
                 "CAST(:f AS text[]), CAST(:d AS jsonb), :c, :x, now() - make_interval(hours => :h)) RETURNING event_id"),
            {"s": student.id, "a": activity, "k": kind, "v": OWNER[activity],
             "st": None if kind in ("WAIVER", "REVERSAL") else "SEED-1", "f": flags, "d": json.dumps(details),
             "c": cycle, "x": corrects, "h": hours_ago},
        ).scalar_one()


def scalar(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()


def rows(engine, sql, **params):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


def physical_row(engine, event_id):
    """The stored row as PostgreSQL holds it: its content hash AND the physical tuple identity. An UPDATE of any
    kind (even one that changes nothing) creates a new tuple version, so xmin/ctid would move: this is a much
    stronger check than comparing the columns."""
    return rows(engine, "SELECT xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS content, t::text AS whole "
                        "FROM activity_events t WHERE event_id = :e", e=event_id)[0]


def table_fingerprint(engine, table, order="1", where="true"):
    return scalar(engine, f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY {order}), '')) FROM {table} t WHERE {where}")


def parse_csv(content: bytes):
    """Decode as Excel would: UTF-8 with a BOM. Returns (header, list of dict rows)."""
    assert content.startswith(b"\xef\xbb\xbf"), "CSV must carry a UTF-8 byte-order mark so Excel reads Unicode names correctly"
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    data = list(reader)
    header = data[0]
    return header, [dict(zip(header, r)) for r in data[1:]]


def error_code(response):
    return response.json()["detail"]["code"]

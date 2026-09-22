"""Helpers shared by the Admin test modules (Phases 13 and 16). Rows are inserted DIRECTLY as SQL, so the
expected answers in the tests never depend on the code under test.
"""
import csv
import io
import json
from types import SimpleNamespace

from sqlalchemy import text

from backend.stage import state as stage_state

from tests.test_auth import ACTIVITIES
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
            text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, "
                 "completion_cycle, corrects_event_id, server_time) VALUES (:s, :a, :k, gen_random_uuid(), "
                 "CAST(:f AS text[]), CAST(:d AS jsonb), :c, :x, now() - make_interval(hours => :h)) RETURNING event_id"),
            {"s": student.id, "a": activity, "k": kind,
             "f": flags, "d": json.dumps(details),
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


# --------------------------------------------------------------------------- the constructed dataset (Phase 13 + 16)
S1, S2, S3 = "School of Engineering", "School of Law", "School of Arts"
ORDER = list(ACTIVITIES)


class Person(SimpleNamespace):
    """A student and what the model says is true of them."""


# 22 students with a KNOWN answer: never registered, reversed registration, every stage of the journey, skips, a
# waiver, a reversed return, flagged events, queue rows, a student on stage, exceptions, scan attempts and outbox rows.
# Used by the report tests and by the restore drill (which must regenerate every report identically).
def build_dataset(engine):
    people: dict[str, Person] = {}

    def person(label, school, name=None, steps=0, flags=None, then=()):
        s = add_student(engine, school=school, name=name)
        p = Person(label=label, s=s, school=school, active=set(), events={}, registration_event=False, prn=s.prn, name=s.name)
        for activity in ORDER[:steps]:
            p.events[activity] = add_event(engine, s, activity, flags=(flags or {}).get(activity, ()))
            p.active.add(activity)
        p.registration_event = steps >= 1
        for step in then:
            step(p)
        people[label] = p
        return p

    def reversal(activity, reason):
        def do(p):
            add_event(engine, p.s, activity, kind="REVERSAL", corrects=p.events[activity], details={"reason": reason})
            p.active.discard(activity)
        return do

    def skip(reason, at="before"):
        def do(p):
            add_event(engine, p.s, "STAGE", kind="SKIP", details={"reason": reason})
        return do

    # A: never registered (no Registration event of any kind). Names chosen to break naive exports.
    person("A1", S1, "अनिल कुमार")
    person("A2", S1, "José Müller")
    person("A3", S2, "李小龍")
    person("A4", S3, "=SUM(1+1)")
    # B: registered, then an Admin reversed it. Has a Registration event, so NOT "Not Attended".
    person("B", S2, "Siobhán O'Brien", steps=1, then=[reversal("REGISTRATION", "wrong student scanned")])
    person("C1", S1, steps=1, flags={"REGISTRATION": ["LATE"]})
    person("C2", S2, steps=1, flags={"REGISTRATION": ["MANUAL"]})
    person("C3", S3, steps=1)
    person("D1", S1, steps=2)
    person("D2", S3, steps=2)
    person("E1", S1, steps=3, flags={"REGISTRATION": ["LATE"]})
    person("E2", S2, steps=3)
    person("F1", S2, steps=4)
    person("F2", S3, steps=4, flags={"QUEUE": ["MANUAL"]})
    person("G1", S1, steps=4, then=[skip("microphone problem"), lambda p: (p.events.__setitem__("STAGE", add_event(engine, p.s, "STAGE")), p.active.add("STAGE"))])
    person("G2", S1, steps=5)
    person("H", S3, steps=4, then=[skip("not ready")])
    person("I", S1, steps=6, flags={"THOBE_RETURN": ["PROVISIONAL"]})
    person("J", S2, steps=5, then=[lambda p: (add_event(engine, p.s, "THOBE_RETURN", kind="WAIVER", details={
        "reason": "lost thobe", "thobe_allocation_on_record": True}), p.active.add("THOBE_RETURN"))])
    person("K1", S3, steps=7)
    person("K2", S1, steps=7)
    person("L", S1, steps=6, then=[reversal("THOBE_RETURN", "returned to the wrong desk")])

    # --- things the dashboard counts from other tables (all written as raw rows) ---
    F1, F2 = people["F1"], people["F2"]
    with engine.begin() as c:
        c.execute(text("INSERT INTO queue (student_id, status) VALUES (:a, 'QUEUED'), (:b, 'DISPLAYED')"), {"a": F1.s.id, "b": F2.s.id})
        c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school) VALUES (:s, 'Frank Two-Display', 'B.Tech', 'Arts')"),
                  {"s": F2.s.id})
        stage_state.begin_controller_txn(c)
        stage_state.update_state(c, current_student_id=F2.s.id, display_student_id=F2.s.id)
        for result, n in (("DUPLICATE", 3), ("REJECTED", 2), ("SUCCESS", 4)):
            for _ in range(n):
                c.execute(text("INSERT INTO scan_log (activity, result) VALUES ('SEATING', :r)"), {"r": result})
        j_event = c.execute(text("SELECT event_id FROM activity_events WHERE student_id = :s AND kind = 'WAIVER'"), {"s": people["J"].s.id}).scalar_one()
        c.execute(text("INSERT INTO exceptions (type, student_id, event_id, details) VALUES ('RETURN_WAIVED', :s, :e, "
                       "CAST('{\"reason\": \"lost thobe\"}' AS jsonb))"), {"s": people["J"].s.id, "e": j_event})
        c.execute(text("INSERT INTO exceptions (type) VALUES ('CONFLICT')"))
        c.execute(text("INSERT INTO exceptions (type, status, resolved_at) VALUES ('SEQ_GAP', 'RESOLVED', now())"))
    return SimpleNamespace(people=people, all=list(people.values()))

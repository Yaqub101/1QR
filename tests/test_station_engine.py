"""Phase 6 - the station engine (scan -> verify -> confirm), all seven activities.

Golden rules under test (AGENTS.md): 2 station decides activity, 3 duplicates per
activity, 4 venue ownership, 5 append-only, 6 event + outbox in ONE transaction and
success shown only after commit, 8 same-venue prerequisites are hard blocks, 11 plain
one-sentence operator messages.

Everything an activity does is *configuration*; these tests drive one generic engine
through a table of the seven activities. The expected prerequisites, messages and
labels below are written out by hand from SYSTEM_SPEC sections 3, 5, 14 and TODO.md, so
the engine's configuration is checked against the spec rather than against itself.

Tokens are inserted directly: token generation is Phase 4.
"""
import dataclasses
import itertools
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from backend import stations as stations_svc
from backend import users as users_svc
from backend.config import Settings
from backend.engine import cross_venue, service
from backend.engine.activities import ACTIVITY_CONFIGS
from backend.engine.model import ActivityConfig, Prerequisite, RegistryError, validate_registry
from backend.main import create_app
from tests.conftest import TEST_DB_URL
from tests.test_auth import ACTIVITIES, OWNER, PASSWORD, VENUES, api_login, new_client, slug
from tests.test_schema import REPO_ROOT, STATUS_AFTER_STEP, _run_threads, drop_everything, run_alembic

IST = timezone(timedelta(minutes=330))  # the default event clock (Settings.event_utc_offset_minutes)

STATION = {  # the one station each operator sits at
    "REGISTRATION": "REG-01", "THOBE_ALLOCATION": "THO-01", "SEATING": "SEA-01", "QUEUE": "QUE-01",
    "STAGE": "STG-01", "THOBE_RETURN": "RET-01", "LUNCH": "LUN-01",
}

# --- hand-written from SYSTEM_SPEC section 5 / TODO Phase 12 / section 14 -----------------------
PREREQ = {
    "REGISTRATION": [],
    "THOBE_ALLOCATION": ["REGISTRATION"],
    "SEATING": ["THOBE_ALLOCATION"],
    "QUEUE": ["SEATING"],
    "STAGE": ["QUEUE"],
    "THOBE_RETURN": ["STAGE", "THOBE_ALLOCATION"],  # section 14: "NO THOBE WAS ISSUED" needs the allocation too
    "LUNCH": ["THOBE_RETURN"],
}
# Same-venue prerequisites are hard blocks. THOBE_ALLOCATION (needs College's Registration) and
# THOBE_RETURN (needs Stadium's Stage and Allocation) only have CROSS-venue prerequisites.
HARD_BLOCK_MESSAGE = {
    "SEATING": "SEATING NOT AVAILABLE — THOBE NOT RECEIVED",
    "QUEUE": "QUEUE NOT AVAILABLE — SEATING PENDING",
    "STAGE": "STAGE NOT AVAILABLE — QUEUE PENDING",
    "LUNCH": "LUNCH NOT AVAILABLE — THOBE RETURN PENDING",
}
CROSS_VENUE_ONLY = ["THOBE_ALLOCATION", "THOBE_RETURN"]
CONFIRM_LABEL = {
    "REGISTRATION": "CONFIRM REGISTRATION", "THOBE_ALLOCATION": "CONFIRM THOBE GIVEN",
    "SEATING": "CONFIRM SEATED", "QUEUE": "CONFIRM QUEUE", "STAGE": "COMPLETE",
    "THOBE_RETURN": "CONFIRM RETURN", "LUNCH": "CONFIRM LUNCH",
}
DISPLAY_KEYS = {  # SYSTEM_SPEC section 3, "Operator sees" (photo and name are always shown).
    # The university's real list has no Convocation Sequence Number and no Seat Number, so neither
    # appears anywhere: Seating is a plain seated / not-seated checkpoint like Thobe Allocation, and
    # the Queue runs purely on the order confirmations happen in.
    "REGISTRATION": ["prn", "programme", "school"],
    "THOBE_ALLOCATION": ["prn", "programme", "school"],
    "SEATING": ["prn", "programme", "school"],
    "QUEUE": ["prn", "queue_position"],
    "STAGE": ["programme", "school"],
    "THOBE_RETURN": ["prn", "thobe_issued"],
    "LUNCH": ["prn", "eligibility"],
}
UNKNOWN_QR = "QR NOT RECOGNISED — use PRN search or contact Admin"
NOT_FOUND = "STUDENT NOT FOUND — CONTACT ADMIN"
INACTIVE = "STUDENT NOT ACTIVE — CONTACT ADMIN"


def duplicate_message(activity, *, time, position=None):
    return {
        "REGISTRATION": f"ALREADY REGISTERED — {time}",
        "THOBE_ALLOCATION": f"THOBE ALREADY ALLOCATED — {time}",
        "SEATING": f"SEATING ALREADY CONFIRMED — {time}",
        "QUEUE": f"ALREADY IN QUEUE — POSITION {position} — {time}",
        "STAGE": f"DEGREE ALREADY RECEIVED — {time}",
        "THOBE_RETURN": f"ALREADY RETURNED — {time}",
        "LUNCH": f"LUNCH ALREADY CLAIMED — {time}",
    }[activity]


def clock(dt):
    dt = dt.astimezone(IST)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


TECH_WORDS = ("traceback", "exception", "error", "sql", "constraint", "postgres", "psycopg", "integrity",
              "violation", "stack", "null", "none", "http", "23505", "uuid", "sqlalchemy")


def assert_plain(message):
    """One plain sentence: no technical detail, code, id or stack trace (golden rule 11)."""
    assert isinstance(message, str) and message.strip(), message
    assert "\n" not in message and len(message) <= 120, message
    low = message.lower()
    for word in TECH_WORDS:
        assert word not in low, f"{word!r} leaked into operator message: {message!r}"
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", low), f"an id leaked: {message!r}"
    assert ". " not in message, f"more than one sentence: {message!r}"


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_size=30, max_overflow=0, pool_pre_ping=True)
    drop_everything(eng)
    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr
    yield eng
    drop_everything(eng)
    eng.dispose()


@pytest.fixture(scope="module")
def world(engine):
    w = SimpleNamespace(user_ids={}, devices={}, op_ids={})
    with engine.begin() as c:
        w.admin_id = users_svc.create_user(c, username="eng-admin", password=PASSWORD, role="ADMIN")
        for activity in ACTIVITIES:
            w.op_ids[activity] = users_svc.create_user(
                c, username=f"eng-{activity.lower()}", password=PASSWORD, role=activity)
            stations_svc.create_station(c, venue_id=OWNER[activity], station_id=STATION[activity], activity=activity)
            w.devices[activity] = stations_svc.bind_station(c, STATION[activity], actor_id=w.admin_id)
        stations_svc.create_station(c, venue_id="college", station_id="REG-02", activity="REGISTRATION")
        w.devices["REG-02"] = stations_svc.bind_station(c, "REG-02", actor_id=w.admin_id)
    return w


def build_app(mode, venue=None):
    return create_app(settings=Settings(mode=mode, venue_id=venue, database_url=TEST_DB_URL))


@pytest.fixture(scope="module")
def apps(engine, world):
    built = {v: build_app("venue", v) for v in VENUES}
    built["central"] = build_app("central")
    return built


_CLIENTS = {}


def operator(apps, world, activity):
    if activity not in _CLIENTS:
        client = new_client(apps[OWNER[activity]], world.devices[activity])
        assert api_login(client, f"eng-{activity.lower()}").status_code == 200
        _CLIENTS[activity] = client
    return _CLIENTS[activity]


def admin(apps, venue):
    key = ("admin", venue)
    if key not in _CLIENTS:
        client = new_client(apps[venue])
        assert api_login(client, "eng-admin").status_code == 200
        _CLIENTS[key] = client
    return _CLIENTS[key]


_SEQ = itertools.count(7_000_000)


def make_student(engine, *, active=True, seat_no=None, token=True, photo_path=None, name=None):
    n = next(_SEQ)
    with engine.begin() as c:
        sid = c.execute(
            text("INSERT INTO students (prn, name, programme, school, sequence_no, seat_no, status, photo_path) "
                 "VALUES (:prn, :name, 'B.Tech Computer Science', 'School of Engineering', :n, :seat, :st, :photo) "
                 "RETURNING id"),
            {"prn": f"E{n}", "name": name or f"Student {n}", "n": n, "seat": seat_no,
             "st": "ACTIVE" if active else "INACTIVE", "photo": photo_path},
        ).scalar_one()
        tok = None
        if token:
            tok = uuid.uuid4().hex + uuid.uuid4().hex[:6]
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t)"), {"s": sid, "t": tok})
    return SimpleNamespace(id=sid, prn=f"E{n}", token=tok, seq=n, name=name or f"Student {n}", seat_no=seat_no)


def seed_events(engine, student, activities, *, kind_overrides=None):
    """Insert already-completed activities directly (as if recorded earlier / synced in)."""
    kind_overrides = kind_overrides or {}
    ids = {}
    with engine.begin() as c:
        for activity in activities:
            kind = kind_overrides.get(activity, "COMPLETE")
            ids[activity] = c.execute(
                text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details) "
                     "VALUES (:s, :a, :k, :v, 'SEED-1', gen_random_uuid(), CAST(:f AS text[]), CAST(:d AS jsonb)) RETURNING event_id"),
                {"s": student.id, "a": activity, "k": kind, "v": OWNER[activity],
                 "f": ["CORRECTED"] if kind == "WAIVER" else [], "d": '{"reason": "seeded"}' if kind != "COMPLETE" else "{}"},
            ).scalar_one()
    return ids


def ready_student(engine, activity, **kw):
    """A student who has completed everything before `activity` in the journey."""
    s = make_student(engine, **kw)
    seed_events(engine, s, ACTIVITIES[:ACTIVITIES.index(activity)])
    return s


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).mappings().all()


def events_of(engine, student, activity=None):
    sql = "SELECT * FROM activity_events WHERE student_id = :s"
    if activity:
        return q(engine, sql + " AND activity = :a AND kind IN ('COMPLETE','WAIVER') ORDER BY venue_seq", s=student.id, a=activity)
    return q(engine, sql + " ORDER BY venue_seq", s=student.id)


def log_of(engine, student, activity=None):
    sql = "SELECT * FROM scan_log WHERE student_id = :s"
    if activity:
        return q(engine, sql + " AND activity = :a ORDER BY id", s=student.id, a=activity)
    return q(engine, sql + " ORDER BY id", s=student.id)


def totals(engine):
    return tuple(q(engine, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in ("activity_events", "outbox", "queue"))


def scan(client, activity, token, **extra):
    return client.post("/scan", json={"token": token, "station_id": STATION[activity], **extra})


def confirm(client, activity, *, token=None, student_id=None, **extra):
    body = {"station_id": STATION[activity], **extra}
    if token is not None:
        body["token"] = token
    if student_id is not None:
        body["student_id"] = str(student_id)
    return client.post("/confirm", json=body)


def field(card, key):
    return next((f["value"] for f in card["fields"] if f["key"] == key), None)


# --------------------------------------------------------------------------- #
# The configuration itself
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_exactly_the_seven_activities_are_configured(self):
        assert sorted(ACTIVITY_CONFIGS) == sorted(ACTIVITIES)

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_prerequisites_match_the_spec(self, activity):
        assert [p.activity for p in ACTIVITY_CONFIGS[activity].prerequisites] == PREREQ[activity]

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_owning_venue_is_the_single_writer_venue(self, activity):
        assert ACTIVITY_CONFIGS[activity].owning_venue == OWNER[activity]

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_same_venue_and_cross_venue_prerequisites_are_told_apart_automatically(self, activity):
        cfg = ACTIVITY_CONFIGS[activity]
        assert [p.activity for p in cfg.same_venue_prerequisites()] == [a for a in PREREQ[activity] if OWNER[a] == OWNER[activity]]
        assert [p.activity for p in cfg.cross_venue_prerequisites()] == [a for a in PREREQ[activity] if OWNER[a] != OWNER[activity]]

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_labels_and_fields_are_the_specified_ones(self, activity):
        cfg = ACTIVITY_CONFIGS[activity]
        assert cfg.confirm_label == CONFIRM_LABEL[activity]
        assert list(cfg.display_fields) == DISPLAY_KEYS[activity]

    @pytest.mark.parametrize("activity", sorted(HARD_BLOCK_MESSAGE))
    def test_hard_block_messages_are_the_specified_ones(self, activity):
        assert [p.missing_message for p in ACTIVITY_CONFIGS[activity].same_venue_prerequisites()] == [HARD_BLOCK_MESSAGE[activity]]

    def test_the_hand_written_tables_at_the_top_of_this_file_are_complete_and_consistent(self):
        """When an activity is added or changed, every table below must be updated together. This catches
        a forgotten row (see docs/STATION_CONTRACT.md section 7, step 4)."""
        for table in (PREREQ, CONFIRM_LABEL, DISPLAY_KEYS, STATION):
            assert sorted(table) == sorted(ACTIVITIES)
        with_same_venue = sorted(a for a in ACTIVITIES if any(OWNER[p] == OWNER[a] for p in PREREQ[a]))
        assert sorted(HARD_BLOCK_MESSAGE) == with_same_venue  # a hard-block row iff a same-venue prerequisite
        cross_only = sorted(a for a in ACTIVITIES if PREREQ[a] and all(OWNER[p] != OWNER[a] for p in PREREQ[a]))
        assert sorted(CROSS_VENUE_ONLY) == cross_only          # the cross-venue-only activities, listed explicitly
        for activity in ACTIVITIES:  # the duplicate template exists and formats for every activity
            assert_plain(duplicate_message(activity, time="11:21 AM", position=1))

    def test_the_shipped_registry_passes_its_own_validation(self):
        validate_registry(ACTIVITY_CONFIGS)

    @pytest.mark.parametrize("mutate,why", [
        (lambda c: dataclasses.replace(c["SEATING"], owning_venue="hall"), "owner"),
        (lambda c: dataclasses.replace(c["SEATING"], prerequisites=(Prerequisite("EXIT", "X — Y"),)), "unknown prerequisite"),
        (lambda c: dataclasses.replace(c["SEATING"], prerequisites=(Prerequisite("SEATING", "X — Y"),)), "self prerequisite"),
        (lambda c: dataclasses.replace(c["SEATING"], prerequisites=(Prerequisite("LUNCH", "X — Y"),)), "later activity"),
        (lambda c: dataclasses.replace(c["SEATING"], display_fields=("prn", "phone")), "unknown display field"),
        (lambda c: dataclasses.replace(c["SEATING"], effects=("teleport",)), "unknown effect"),
        (lambda c: dataclasses.replace(c["SEATING"], flag_rules=("sometimes",)), "unknown flag rule"),
        (lambda c: dataclasses.replace(c["SEATING"], record_fields=("password_hash",)), "unknown recorded field"),
        (lambda c: dataclasses.replace(c["SEATING"], confirm_label="  "), "blank label"),
        (lambda c: dataclasses.replace(c["SEATING"], duplicate_message="DONE — {when}"), "unknown placeholder"),
        (lambda c: dataclasses.replace(c["SEATING"], duplicate_message="DONE.\nAgain."), "multi-line message"),
        (lambda c: dataclasses.replace(c["SEATING"], prerequisites=(Prerequisite("THOBE_ALLOCATION", "line one\nline two"),)), "multi-line prerequisite message"),
        (lambda c: {k: v for k, v in c.items() if k != "LUNCH"}, "missing activity"),
    ])
    def test_a_mistaken_configuration_is_refused_at_startup(self, mutate, why):
        broken = dict(ACTIVITY_CONFIGS)
        changed = mutate(dict(ACTIVITY_CONFIGS))
        if isinstance(changed, ActivityConfig):  # one entry was replaced: put it back into the registry
            broken[changed.activity] = changed
        else:
            broken = changed
        with pytest.raises(RegistryError):
            validate_registry(broken)

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_every_message_in_the_configuration_is_plain(self, activity):
        cfg = ACTIVITY_CONFIGS[activity]
        sample = cfg.duplicate_message.format_map({"time": "11:21 AM", "station": "X-1", "queue_position": 3})
        assert_plain(sample)
        for p in cfg.prerequisites:
            assert_plain(p.missing_message)


# --------------------------------------------------------------------------- #
# Table-driven pipeline: 7 activities x {pending, done, prerequisite missing, inactive, unknown token}
# --------------------------------------------------------------------------- #
class TestPipelineTable:
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_pending_student_scans_shows_the_card_and_confirm_writes_exactly_one_row(self, apps, world, engine, activity):
        s = ready_student(engine, activity, seat_no="B-12")
        client = operator(apps, world, activity)
        before = totals(engine)

        response = scan(client, activity, s.token)
        body = response.json()
        assert response.status_code == 200 and body["result"] == "READY" and body["colour"] == "blue"
        assert body["activity"] == activity and body["station_id"] == STATION[activity] and body["manual"] is False
        card = body["student"]
        assert card["name"] == s.name and card["photo_url"] == f"/photo/{s.id}"
        assert [f["key"] for f in card["fields"]] == DISPLAY_KEYS[activity]
        # A preview records no ACTIVITY (no event, no outbox row, no queue row) -- but the attempt itself
        # is logged like every other attempt (TODO Phase 6: "Every attempt written to scan_log").
        assert totals(engine) == before
        assert [(r["result"], r["event_id"]) for r in log_of(engine, s, activity)] == [("READY", None)]

        done = confirm(client, activity, token=s.token)
        result = done.json()
        assert done.status_code == 200 and result["result"] == "CONFIRMED" and result["colour"] == "green"

        rows = events_of(engine, s, activity)
        assert len(rows) == 1
        event = rows[0]
        assert (event["kind"], event["venue_id"], event["station_id"]) == ("COMPLETE", OWNER[activity], STATION[activity])
        assert event["operator_id"] == world.op_ids[activity] and event["completion_cycle"] == 1
        assert "MANUAL" not in event["flags"] and "PROVISIONAL" not in event["flags"]
        outbox = q(engine, "SELECT payload FROM outbox WHERE event_id = :e", e=event["event_id"])
        assert len(outbox) == 1 and outbox[0]["payload"]["event_id"] == str(event["event_id"])
        assert outbox[0]["payload"]["student_id"] == str(s.id) and outbox[0]["payload"]["activity"] == activity
        audit = q(engine, "SELECT operator_id, station_id FROM audit_log WHERE event_id = :e AND action = 'ACTIVITY_CONFIRMED'", e=event["event_id"])
        assert len(audit) == 1 and audit[0]["operator_id"] == world.op_ids[activity]
        log = log_of(engine, s, activity)
        assert [(r["result"], r["event_id"]) for r in log] == [("READY", None), ("SUCCESS", event["event_id"])]
        assert all((r["venue_id"], r["station_id"], r["activity"]) == (OWNER[activity], STATION[activity], activity) for r in log)

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_already_done_shows_the_earlier_record_and_writes_nothing(self, apps, world, engine, activity):
        s = ready_student(engine, activity, seat_no="B-12")
        client = operator(apps, world, activity)
        assert confirm(client, activity, token=s.token).json()["result"] == "CONFIRMED"
        event = events_of(engine, s, activity)[0]
        position = None
        if activity == "QUEUE":
            position = q(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=s.id)[0]["queue_position"]
        before = totals(engine)

        response = scan(client, activity, s.token)
        body = response.json()
        expected = duplicate_message(activity, time=clock(event["server_time"]), position=position)
        assert response.status_code == 200 and body["result"] == "DUPLICATE" and body["colour"] == "amber"
        assert body["message"] == expected
        assert body["earlier"]["station_id"] == STATION[activity] and body["earlier"]["time"] == clock(event["server_time"])
        assert body["student"]["name"] == s.name  # the operator can see who this is
        again = confirm(client, activity, token=s.token).json()  # even a forced confirm changes nothing
        assert again["result"] == "DUPLICATE" and again["message"] == expected
        assert totals(engine) == before and len(events_of(engine, s, activity)) == 1

    @pytest.mark.parametrize("activity", sorted(HARD_BLOCK_MESSAGE))
    def test_missing_same_venue_prerequisite_is_a_hard_block(self, apps, world, engine, activity):
        idx = ACTIVITIES.index(activity)
        s = make_student(engine)
        seed_events(engine, s, ACTIVITIES[:idx - 1])  # everything except the direct prerequisite
        client = operator(apps, world, activity)
        before = totals(engine)

        body = scan(client, activity, s.token).json()
        assert body["result"] == "REJECTED" and body["colour"] == "red" and body["message"] == HARD_BLOCK_MESSAGE[activity]
        assert_plain(body["message"])
        forced = confirm(client, activity, token=s.token).json()  # the server re-checks at confirm too
        assert forced["result"] == "REJECTED" and forced["message"] == HARD_BLOCK_MESSAGE[activity]
        assert totals(engine) == before and events_of(engine, s, activity) == []
        assert [r["result"] for r in log_of(engine, s, activity)] == ["REJECTED", "REJECTED"]

    @pytest.mark.parametrize("activity", sorted(HARD_BLOCK_MESSAGE))
    def test_a_student_with_no_history_at_all_is_blocked_on_the_nearest_prerequisite(self, apps, world, engine, activity):
        s = make_student(engine)
        body = scan(operator(apps, world, activity), activity, s.token).json()
        assert body["result"] == "REJECTED" and body["message"] == HARD_BLOCK_MESSAGE[activity]

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_inactive_student_is_rejected(self, apps, world, engine, activity):
        s = ready_student(engine, activity, active=False)
        client = operator(apps, world, activity)
        before = totals(engine)
        for body in (scan(client, activity, s.token).json(), confirm(client, activity, token=s.token).json()):
            assert body["result"] == "REJECTED" and body["colour"] == "red" and body["message"] == INACTIVE
            assert_plain(body["message"])
        assert totals(engine) == before and events_of(engine, s, activity) == []
        assert [r["result"] for r in log_of(engine, s, activity)] == ["REJECTED", "REJECTED"]

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_unknown_token_is_invalid_logged_and_writes_nothing(self, apps, world, engine, activity):
        client = operator(apps, world, activity)
        bogus = "not-a-real-token-" + uuid.uuid4().hex
        before = totals(engine)
        for body in (scan(client, activity, bogus).json(), confirm(client, activity, token=bogus).json()):
            assert body["result"] == "INVALID" and body["colour"] == "red" and body["message"] == UNKNOWN_QR
            assert body["student"] is None
        assert totals(engine) == before
        rows = q(engine, "SELECT result, activity, station_id, token_presented FROM scan_log WHERE token_presented = :t ORDER BY id", t=bogus)
        assert [r["result"] for r in rows] == ["INVALID", "INVALID"]
        assert all(r["activity"] == activity and r["station_id"] == STATION[activity] for r in rows)

    def test_a_replaced_qr_is_refused_with_its_own_message(self, apps, world, engine):
        s = ready_student(engine, "SEATING")
        with engine.begin() as c:
            c.execute(text("UPDATE qr_tokens SET active = false, deactivated_at = now(), deactivated_by = :u WHERE token = :t"),
                      {"u": world.admin_id, "t": s.token})
        body = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()
        assert body["result"] == "INVALID" and body["message"] == "THIS QR HAS BEEN REPLACED — CONTACT ADMIN"
        assert events_of(engine, s, "SEATING") == []

    @pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", "\t", "  ", "\x00"])
    def test_a_scanner_suffix_never_makes_a_good_token_invalid(self, apps, world, engine, suffix):
        s = ready_student(engine, "REGISTRATION")
        body = scan(operator(apps, world, "REGISTRATION"), "REGISTRATION", s.token + suffix).json()
        assert body["result"] == "READY"

    def test_blank_and_oversized_tokens_are_invalid_not_errors(self, apps, world):
        client = operator(apps, world, "REGISTRATION")
        for token in ("", "   ", "x" * 5000):
            body = scan(client, "REGISTRATION", token).json()
            assert body["result"] == "INVALID" and body["message"] == UNKNOWN_QR


# --------------------------------------------------------------------------- #
# Generic behaviour across activities
# --------------------------------------------------------------------------- #
class TestJourney:
    def test_the_same_qr_passes_all_seven_activities_in_order_with_no_false_duplicates(self, apps, world, engine):
        s = make_student(engine, seat_no="C-3")
        seen = []
        for i, activity in enumerate(ACTIVITIES):
            client = operator(apps, world, activity)
            assert scan(client, activity, s.token).json()["result"] == "READY", activity
            assert confirm(client, activity, token=s.token).json()["result"] == "CONFIRMED", activity
            seen.append(q(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id)[0]["status"])
        assert seen == STATUS_AFTER_STEP[1:]
        assert [e["activity"] for e in events_of(engine, s)] != [] and len(events_of(engine, s)) == 7
        assert sorted(e["activity"] for e in events_of(engine, s)) == sorted(ACTIVITIES)
        results = [r["result"] for r in log_of(engine, s)]
        assert results == ["READY", "SUCCESS"] * 7 and "DUPLICATE" not in results   # each step: the scan, then the confirm
        assert q(engine, "SELECT count(*) AS n FROM outbox o JOIN activity_events e USING (event_id) WHERE e.student_id = :s", s=s.id)[0]["n"] == 7

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_same_activity_twice_gives_one_success_and_one_duplicate_log_row(self, apps, world, engine, activity):
        s = ready_student(engine, activity)
        client = operator(apps, world, activity)
        assert scan(client, activity, s.token).json()["result"] == "READY"
        assert confirm(client, activity, token=s.token).json()["result"] == "CONFIRMED"
        assert scan(client, activity, s.token).json()["result"] == "DUPLICATE"  # the second attempt stops here
        assert [r["result"] for r in log_of(engine, s, activity)] == ["READY", "SUCCESS", "DUPLICATE"]
        assert len(events_of(engine, s, activity)) == 1

    def test_a_different_activity_is_never_a_false_duplicate(self, apps, world, engine):
        s = ready_student(engine, "SEATING")
        assert confirm(operator(apps, world, "SEATING"), "SEATING", token=s.token).json()["result"] == "CONFIRMED"
        assert scan(operator(apps, world, "QUEUE"), "QUEUE", s.token).json()["result"] == "READY"

    def test_an_admin_waiver_counts_as_the_return_for_lunch(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ACTIVITIES[:5] + ["THOBE_RETURN"], kind_overrides={"THOBE_RETURN": "WAIVER"})
        assert scan(operator(apps, world, "LUNCH"), "LUNCH", s.token).json()["result"] == "READY"
        again = scan(operator(apps, world, "THOBE_RETURN"), "THOBE_RETURN", s.token).json()
        assert again["result"] == "DUPLICATE" and again["message"].startswith("ALREADY RETURNED — ")

    def test_after_an_admin_reversal_the_activity_can_be_recorded_again_as_cycle_two(self, apps, world, engine):
        s = ready_student(engine, "SEATING")
        first = seed_events(engine, s, ["SEATING"])["SEATING"]
        with engine.begin() as c:
            c.execute(text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, details, "
                           "completion_cycle, corrects_event_id, flags) VALUES (:s, 'SEATING', 'REVERSAL', 'stadium', NULL, "
                           "gen_random_uuid(), '{\"reason\": \"wrong student\"}', 1, :e, '{}')"), {"s": s.id, "e": first})
        client = operator(apps, world, "SEATING")
        assert scan(client, "SEATING", s.token).json()["result"] == "READY"  # not a duplicate any more
        assert confirm(client, "SEATING", token=s.token).json()["result"] == "CONFIRMED"
        rows = q(engine, "SELECT kind, completion_cycle FROM activity_events WHERE student_id = :s AND activity = 'SEATING' ORDER BY venue_seq", s=s.id)
        assert [(r["kind"], r["completion_cycle"]) for r in rows] == [("COMPLETE", 1), ("REVERSAL", 1), ("COMPLETE", 2)]


class TestConcurrency:
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_concurrent_confirms_leave_exactly_one_event(self, apps, world, engine, activity):
        s = ready_student(engine, activity)
        token = operator(apps, world, activity).cookies.get("session")
        n = 8

        def work(_):
            client = new_client(apps[OWNER[activity]])
            client.headers["Authorization"] = f"Bearer {token}"
            return client.post("/confirm", json={"station_id": STATION[activity], "token": s.token}).json()["result"]

        results = _run_threads(work, n)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert sorted(results) == ["CONFIRMED"] + ["DUPLICATE"] * (n - 1)
        assert len(events_of(engine, s, activity)) == 1  # the Phase 2 unique index, not a mocked lock
        assert q(engine, "SELECT count(*) AS n FROM outbox WHERE event_id = :e", e=events_of(engine, s, activity)[0]["event_id"])[0]["n"] == 1
        assert sorted(r["result"] for r in log_of(engine, s, activity)) == ["DUPLICATE"] * (n - 1) + ["SUCCESS"]
        if activity == "QUEUE":
            assert q(engine, "SELECT count(*) AS n FROM queue WHERE student_id = :s", s=s.id)[0]["n"] == 1

    def test_losing_a_race_leaves_no_gap_in_the_venue_sequence(self, apps, world, engine):
        s = ready_student(engine, "LUNCH")
        token = operator(apps, world, "LUNCH").cookies.get("session")

        def work(_):
            client = new_client(apps["hall"])
            client.headers["Authorization"] = f"Bearer {token}"
            return client.post("/confirm", json={"station_id": "LUN-01", "token": s.token}).json()["result"]

        _run_threads(work, 6)
        seqs = [r["venue_seq"] for r in q(engine, "SELECT venue_seq FROM activity_events WHERE venue_id = 'hall' ORDER BY venue_seq")]
        assert seqs == list(range(1, len(seqs) + 1))


class TestAtomicity:
    """Golden rule 6: the event, its outbox row, the audit row, any effect and the scan_log row
    all commit together or not at all. The operator sees success only after the commit."""

    @pytest.mark.parametrize("failing_step", ["insert_outbox", "insert_audit"])
    @pytest.mark.parametrize("activity", ["REGISTRATION", "QUEUE"])
    def test_a_failure_after_the_event_insert_leaves_nothing_behind(self, apps, world, engine, monkeypatch, failing_step, activity):
        s = ready_student(engine, activity)
        client = operator(apps, world, activity)
        before = totals(engine)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash after the event insert")

        monkeypatch.setattr(service, failing_step, boom)
        response = confirm(client, activity, token=s.token)
        assert response.status_code == 503 and response.json()["detail"]["message"] == "One moment, please try again."
        assert totals(engine) == before                      # no event, no outbox row, no queue row
        assert events_of(engine, s, activity) == []
        assert log_of(engine, s, activity) == []              # not even a SUCCESS in the log
        assert q(engine, "SELECT count(*) AS n FROM audit_log WHERE action = 'ACTIVITY_CONFIRMED' AND student_id = :s", s=s.id)[0]["n"] == 0

        monkeypatch.undo()                                    # the retry works cleanly
        assert confirm(client, activity, token=s.token).json()["result"] == "CONFIRMED"
        assert len(events_of(engine, s, activity)) == 1

    def test_a_failure_before_any_write_is_also_clean(self, apps, world, engine, monkeypatch):
        s = ready_student(engine, "SEATING")
        monkeypatch.setattr(service, "insert_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
        assert confirm(operator(apps, world, "SEATING"), "SEATING", token=s.token).status_code == 503
        assert events_of(engine, s, "SEATING") == []

    def test_a_failed_attempt_does_not_burn_a_venue_sequence_number(self, apps, world, engine, monkeypatch):
        a, b = ready_student(engine, "REGISTRATION"), ready_student(engine, "REGISTRATION")
        client = operator(apps, world, "REGISTRATION")
        confirm(client, "REGISTRATION", token=a.token)
        last = events_of(engine, a, "REGISTRATION")[0]["venue_seq"]
        monkeypatch.setattr(service, "insert_outbox", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        confirm(client, "REGISTRATION", token=b.token)
        monkeypatch.undo()
        confirm(client, "REGISTRATION", token=b.token)
        assert events_of(engine, b, "REGISTRATION")[0]["venue_seq"] == last + 1

    def test_killing_the_process_mid_confirm_leaves_nothing_half_written(self, engine, world):
        s = ready_student(engine, "REGISTRATION")
        before = totals(engine)
        script = textwrap.dedent("""
            import os, uuid
            from sqlalchemy import create_engine
            from backend.config import Settings
            from backend.engine import service
            from backend.security.ownership import guard_from_settings
            from backend.security.sessions import Principal

            settings = Settings(mode="venue", venue_id="college", database_url=os.environ["DATABASE_URL"])
            engine = create_engine(settings.database_url)
            principal = Principal(session_id="x", user_id=uuid.UUID(os.environ["OP_ID"]), username="op", full_name=None,
                                  role="REGISTRATION", station_id="REG-01", station_activity="REGISTRATION")

            def die(*args, **kwargs):          # the event is inserted; the process dies before COMMIT
                os._exit(137)

            service.insert_outbox = die
            service.confirm(engine, settings=settings, guard=guard_from_settings(settings), principal=principal,
                            station_id="REG-01", token=os.environ["TOKEN"])
            print("REACHED THE END")
        """)
        env = {**os.environ, "DATABASE_URL": TEST_DB_URL, "OP_ID": str(world.op_ids["REGISTRATION"]), "TOKEN": s.token,
               "MODE": "venue", "VENUE_ID": "college"}
        run = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert run.returncode == 137 and "REACHED THE END" not in run.stdout, run.stdout + run.stderr
        assert totals(engine) == before and events_of(engine, s, "REGISTRATION") == [] and log_of(engine, s) == []


class TestManualSearch:
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_search_by_prn_shows_a_photo_and_the_event_is_flagged_manual(self, apps, world, engine, activity):
        s = ready_student(engine, activity, seat_no="D-9")
        client = operator(apps, world, activity)
        response = client.post("/search", json={"prn": s.prn, "station_id": STATION[activity]})
        body = response.json()
        assert response.status_code == 200 and body["result"] == "READY" and body["manual"] is True
        card = body["student"]
        assert card["name"] == s.name and card["student_id"] == str(s.id) and card["photo_url"] == f"/photo/{s.id}"
        assert client.get(card["photo_url"]).status_code == 200  # the operator can see the photo to verify

        done = confirm(client, activity, student_id=s.id).json()
        assert done["result"] == "CONFIRMED" and done["manual"] is True
        event = events_of(engine, s, activity)[0]
        assert "MANUAL" in event["flags"]
        assert [r["result"] for r in log_of(engine, s, activity)] == ["READY", "MANUAL"]   # the search, then the confirm
        outbox = q(engine, "SELECT payload FROM outbox WHERE event_id = :e", e=event["event_id"])[0]["payload"]
        assert "MANUAL" in outbox["flags"]

    def test_search_is_case_and_space_insensitive_but_prn_only(self, apps, world, engine):
        s = ready_student(engine, "SEATING")
        client = operator(apps, world, "SEATING")
        assert client.post("/search", json={"prn": f"  {s.prn.lower()}\n", "station_id": "SEA-01"}).json()["result"] == "READY"
        assert client.post("/search", json={"prn": s.name, "station_id": "SEA-01"}).json()["result"] == "INVALID"  # never by name

    def test_unknown_prn_is_invalid_and_logged(self, apps, world, engine):
        body = operator(apps, world, "QUEUE").post("/search", json={"prn": "NOPE-0000", "station_id": "QUE-01"}).json()
        assert body["result"] == "INVALID" and body["message"] == NOT_FOUND
        assert q(engine, "SELECT result, prn_entered FROM scan_log WHERE prn_entered = 'NOPE-0000'")[0]["result"] == "INVALID"

    def test_manual_confirm_still_obeys_every_rule(self, apps, world, engine):
        client = operator(apps, world, "SEATING")
        inactive = ready_student(engine, "SEATING", active=False)
        assert confirm(client, "SEATING", student_id=inactive.id).json()["message"] == INACTIVE
        blocked = make_student(engine)
        assert confirm(client, "SEATING", student_id=blocked.id).json()["message"] == HARD_BLOCK_MESSAGE["SEATING"]
        done = ready_student(engine, "SEATING")
        confirm(client, "SEATING", student_id=done.id)
        assert confirm(client, "SEATING", student_id=done.id).json()["result"] == "DUPLICATE"
        assert client.post("/search", json={"prn": done.prn, "station_id": "SEA-01"}).json()["result"] == "DUPLICATE"
        assert len(events_of(engine, done, "SEATING")) == 1

    def test_the_manual_flag_cannot_be_dodged(self, apps, world, engine):
        s = ready_student(engine, "REGISTRATION")
        client = operator(apps, world, "REGISTRATION")
        assert client.post("/confirm", json={"station_id": "REG-01", "token": s.token, "student_id": str(s.id)}).status_code == 422
        assert client.post("/confirm", json={"station_id": "REG-01"}).status_code == 422
        assert client.post("/confirm", json={"station_id": "REG-01", "student_id": str(s.id), "manual": False}).status_code == 200
        assert "MANUAL" in events_of(engine, s, "REGISTRATION")[0]["flags"]  # a student_id is always a manual entry

    def test_a_malformed_student_id_is_not_found_not_an_error(self, apps, world):
        body = confirm(operator(apps, world, "REGISTRATION"), "REGISTRATION", student_id="not-a-uuid").json()
        assert body["result"] == "INVALID" and body["message"] == NOT_FOUND


class TestPhotos:
    def test_photo_is_served_to_signed_in_users_only(self, apps, world, engine):
        s = make_student(engine)
        assert new_client(apps["college"]).get(f"/photo/{s.id}").status_code == 401
        assert operator(apps, world, "REGISTRATION").get(f"/photo/{s.id}").status_code == 200

    def test_a_missing_photo_falls_back_to_the_placeholder(self, apps, world, engine):
        response = operator(apps, world, "REGISTRATION").get(f"/photo/{make_student(engine).id}")
        assert response.status_code == 200 and "svg" in response.headers["content-type"]

    def test_a_linked_photo_file_is_served(self, apps, world, engine, tmp_path):
        picture = tmp_path / "face.jpg"
        picture.write_bytes(b"\xff\xd8\xff\xe0-fake-jpeg-bytes")
        s = make_student(engine, photo_path=str(picture))
        response = operator(apps, world, "REGISTRATION").get(f"/photo/{s.id}")
        assert response.status_code == 200 and response.content == picture.read_bytes()
        assert response.headers["content-type"] == "image/jpeg"

    def test_unknown_or_malformed_ids_are_404(self, apps, world):
        client = operator(apps, world, "REGISTRATION")
        assert client.get(f"/photo/{uuid.uuid4()}").status_code == 404
        assert client.get("/photo/not-a-uuid").status_code == 404


class TestAccessRules:
    @pytest.mark.parametrize("path,body", [
        ("/scan", {"token": "x", "station_id": "REG-01"}), ("/confirm", {"token": "x", "station_id": "REG-01"}),
        ("/search", {"prn": "x", "station_id": "REG-01"}),
    ])
    def test_anonymous_callers_are_refused(self, apps, path, body):
        assert new_client(apps["college"]).post(path, json=body).status_code == 401

    def test_the_activity_comes_from_the_station_never_from_the_request(self, apps, world, engine):
        s = ready_student(engine, "REGISTRATION")
        client = operator(apps, world, "REGISTRATION")
        for smuggled in ({"activity": "LUNCH"}, {"activity": "LUNCH", "venue_id": "hall", "kind": "WAIVER"}):
            body = client.post("/scan", json={"token": s.token, "station_id": "REG-01", **smuggled}).json()
            assert body["activity"] == "REGISTRATION" and body["result"] == "READY"
        done = client.post("/confirm", json={"token": s.token, "station_id": "REG-01", "activity": "LUNCH", "kind": "WAIVER"}).json()
        assert done["result"] == "CONFIRMED"
        event = events_of(engine, s, "REGISTRATION")[0]
        assert (event["activity"], event["kind"], event["venue_id"]) == ("REGISTRATION", "COMPLETE", "college")
        assert events_of(engine, s, "LUNCH") == []

    def test_an_operator_cannot_act_for_another_station(self, apps, world, engine):
        s = make_student(engine)
        client = operator(apps, world, "REGISTRATION")
        other_desk = client.post("/scan", json={"token": s.token, "station_id": "REG-02"})
        assert other_desk.status_code == 403 and other_desk.json()["detail"]["code"] == "STATION_MISMATCH"
        for foreign in ("LUN-01", "SEA-01"):
            assert client.post("/scan", json={"token": s.token, "station_id": foreign}).status_code == 403
        assert client.post("/scan", json={"token": s.token, "station_id": "NOPE-9"}).status_code == 404
        assert log_of(engine, s) == []

    def test_the_role_must_fit_the_station_even_if_the_binding_says_otherwise(self, apps, world, engine):
        # Isolate the role check: point a Registration operator's session at the SEATING station. The
        # station matches the session, but Registration is not a Seating role, so it is still refused.
        username = f"role-{uuid.uuid4().hex[:6]}"
        with engine.begin() as c:
            users_svc.create_user(c, username=username, password=PASSWORD, role="REGISTRATION")
        client = new_client(apps["stadium"], world.devices["SEATING"])
        assert api_login(client, username).status_code == 403  # cannot even sign in at that station...
        client = new_client(apps["college"], world.devices["REGISTRATION"])
        token = api_login(client, username).json()["token"]
        with engine.begin() as c:  # ...so tamper with the session to reach the situation directly
            c.execute(text("UPDATE sessions SET station_id = 'SEA-01' WHERE token_hash = :h"),
                      {"h": __import__("hashlib").sha256(token.encode()).hexdigest()})
        s = ready_student(engine, "SEATING")
        response = new_client(apps["stadium"]).post(
            "/scan", json={"token": s.token, "station_id": "SEA-01"}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "FORBIDDEN"
        assert log_of(engine, s) == []

    def test_admin_can_use_any_station_of_the_servers_own_venue(self, apps, world, engine):
        s = ready_student(engine, "REGISTRATION")
        assert admin(apps, "college").post("/scan", json={"token": s.token, "station_id": "REG-02"}).json()["result"] == "READY"
        assert admin(apps, "stadium").post("/scan", json={"token": s.token, "station_id": "REG-01"}).status_code == 403

    def test_a_venue_refuses_a_station_it_does_not_own(self, apps, world, engine):
        s = make_student(engine)
        response = admin(apps, "hall").post("/scan", json={"token": s.token, "station_id": "REG-01"})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "WRONG_VENUE"
        assert "not here" in response.json()["detail"]["message"]

    def test_central_never_runs_a_station_engine(self, apps, world, engine):
        s = make_student(engine)
        for path, body in (("/scan", {"token": s.token, "station_id": "REG-01"}), ("/confirm", {"token": s.token, "station_id": "REG-01"})):
            response = admin(apps, "central").post(path, json=body)
            assert response.status_code == 403 and response.json()["detail"]["code"] == "CENTRAL_CANNOT_ORIGINATE"
        assert events_of(engine, s) == []

    def test_a_deactivated_station_cannot_be_used(self, apps, world, engine):
        with engine.begin() as c:
            stations_svc.create_station(c, venue_id="college", station_id="REG-OFF", activity="REGISTRATION")
            stations_svc.set_station_active(c, "REG-OFF", False, actor_id=world.admin_id)
        response = admin(apps, "college").post("/scan", json={"token": "x", "station_id": "REG-OFF"})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "STATION_INACTIVE"

    def test_missing_or_wrong_typed_fields_are_422(self, apps, world):
        client = operator(apps, world, "REGISTRATION")
        assert client.post("/scan", json={"station_id": "REG-01"}).status_code == 422
        assert client.post("/scan", json={"token": "x"}).status_code == 422
        assert client.post("/scan", json={"token": 5, "station_id": "REG-01"}).status_code == 422


# --------------------------------------------------------------------------- #
# Cross-venue prerequisites: the real freshness rule (SYSTEM_SPEC 11.5). The full rule, with real sync
# between separate databases, is tested in tests/test_reconcile.py; this class keeps the ENGINE-side wiring honest.
# --------------------------------------------------------------------------- #
class TestCrossVenueHook:
    """The engine calls ONE hook for every cross-venue prerequisite (backend/engine/cross_venue.py)."""

    def test_the_phase_6_stub_is_gone_and_the_real_rule_is_in_its_place(self, apps, world, engine):
        import inspect
        source = inspect.getsource(cross_venue)
        assert not hasattr(cross_venue, "CROSS_VENUE_RULES_IMPLEMENTED")  # the "still a stub" flag no longer exists
        assert "STUB" not in source and "TODO(" not in source and "ALWAYS ALLOWS" not in source.upper()
        # A server that has never synced counts every peer as STALE, so a missing prerequisite is accepted
        # PROVISIONALLY (never blocked: internet failure is not event failure) ...
        s = make_student(engine)
        client = operator(apps, world, "THOBE_ALLOCATION")
        assert scan(client, "THOBE_ALLOCATION", s.token).json()["result"] == "READY"
        assert confirm(client, "THOBE_ALLOCATION", token=s.token).json()["result"] == "CONFIRMED"
        assert "PROVISIONAL" in events_of(engine, s, "THOBE_ALLOCATION")[0]["flags"]
        # ... but the same missing prerequisite is BLOCKED the moment the College is known to be fresh here.
        with engine.begin() as c:
            c.execute(text("INSERT INTO sync_state (peer, data_as_of) VALUES ('college', now()) "
                           "ON CONFLICT (peer) DO UPDATE SET data_as_of = now()"))
        try:
            t = make_student(engine)
            blocked = scan(client, "THOBE_ALLOCATION", t.token).json()
            assert blocked["result"] == "REJECTED" and blocked["message"] == "THOBE NOT AVAILABLE — REGISTRATION PENDING"
            assert confirm(client, "THOBE_ALLOCATION", token=t.token).json()["result"] == "REJECTED"
            assert events_of(engine, t, "THOBE_ALLOCATION") == []
        finally:
            with engine.begin() as c:
                c.execute(text("DELETE FROM sync_state WHERE peer = 'college'"))

    def test_registration_has_no_prerequisites_at_all(self, apps, world, engine):
        assert scan(operator(apps, world, "REGISTRATION"), "REGISTRATION", make_student(engine).token).json()["result"] == "READY"

    @pytest.mark.parametrize("activity,expected", [
        ("THOBE_ALLOCATION", ["REGISTRATION"]), ("THOBE_RETURN", ["STAGE", "THOBE_ALLOCATION"]),
        ("REGISTRATION", []), ("SEATING", []), ("QUEUE", []), ("STAGE", []), ("LUNCH", []),
    ])
    def test_the_hook_is_consulted_exactly_for_cross_venue_prerequisites(self, apps, world, engine, monkeypatch, activity, expected):
        calls = []

        def spy(conn, *, student_id, prerequisite, this_venue, present_locally):
            calls.append((prerequisite.activity, this_venue, present_locally))
            return cross_venue.CrossVenueDecision(allow=True)

        monkeypatch.setattr(cross_venue, "check_cross_venue_prerequisite", spy)
        s = ready_student(engine, activity)
        scan(operator(apps, world, activity), activity, s.token)
        assert [c[0] for c in calls] == expected
        assert all(c[1] == OWNER[activity] and c[2] is True for c in calls)  # present_locally is reported truthfully

    def test_the_hook_reports_a_missing_prerequisite_truthfully(self, apps, world, engine, monkeypatch):
        seen = []
        monkeypatch.setattr(cross_venue, "check_cross_venue_prerequisite",
                            lambda conn, **k: seen.append(k["present_locally"]) or cross_venue.CrossVenueDecision(allow=True))
        scan(operator(apps, world, "THOBE_ALLOCATION"), "THOBE_ALLOCATION", make_student(engine).token)
        assert seen == [False]

    def test_a_blocking_hook_rejects_with_its_own_plain_message(self, apps, world, engine, monkeypatch):
        message = "THOBE NOT AVAILABLE — REGISTRATION PENDING"
        monkeypatch.setattr(cross_venue, "check_cross_venue_prerequisite",
                            lambda conn, **k: cross_venue.CrossVenueDecision(allow=False, message=message))
        s = make_student(engine)
        client = operator(apps, world, "THOBE_ALLOCATION")
        for body in (scan(client, "THOBE_ALLOCATION", s.token).json(), confirm(client, "THOBE_ALLOCATION", token=s.token).json()):
            assert body["result"] == "REJECTED" and body["message"] == message
        assert events_of(engine, s, "THOBE_ALLOCATION") == []
        assert [r["result"] for r in log_of(engine, s)] == ["REJECTED", "REJECTED"]

    def test_a_provisional_hook_is_accepted_with_a_normal_confirmation_and_flagged(self, apps, world, engine, monkeypatch):
        monkeypatch.setattr(cross_venue, "check_cross_venue_prerequisite",
                            lambda conn, **k: cross_venue.CrossVenueDecision(allow=True, provisional=True))
        s = make_student(engine)
        client = operator(apps, world, "THOBE_ALLOCATION")
        ready = scan(client, "THOBE_ALLOCATION", s.token).json()
        done = confirm(client, "THOBE_ALLOCATION", token=s.token).json()
        assert ready["result"] == "READY" and done["result"] == "CONFIRMED" and done["colour"] == "green"
        assert "provisional" not in (done["message"] + ready["message"]).lower()  # the operator sees nothing scary
        assert "PROVISIONAL" in events_of(engine, s, "THOBE_ALLOCATION")[0]["flags"]
        log = log_of(engine, s)
        assert [r["result"] for r in log] == ["READY", "PROVISIONAL"]     # the scan, then the confirm
        assert log[0]["details"]["provisional"] is True                   # the scan row already knew

    def test_same_venue_prerequisites_never_go_through_the_hook(self, apps, world, engine, monkeypatch):
        monkeypatch.setattr(cross_venue, "check_cross_venue_prerequisite",
                            lambda conn, **k: cross_venue.CrossVenueDecision(allow=True, provisional=True))
        s = make_student(engine)  # SEATING needs THOBE_ALLOCATION: same venue, so a hard block whatever the hook says
        assert scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()["result"] == "REJECTED"


# --------------------------------------------------------------------------- #
# Extension points that keep Phases 7-12 configuration-only
# --------------------------------------------------------------------------- #
class TestConfiguredBehaviours:
    def _set_cutoff(self, engine, sql):
        with engine.begin() as c:
            c.execute(text(f"UPDATE settings SET late_cutoff = {sql}"))

    @pytest.mark.parametrize("cutoff,flags", [("NULL", []), ("now() + interval '1 hour'", []), ("now() - interval '1 hour'", ["LATE"])])
    def test_registration_after_the_cutoff_is_accepted_and_flagged_late(self, apps, world, engine, cutoff, flags):
        try:
            self._set_cutoff(engine, cutoff)
            s = make_student(engine)
            assert confirm(operator(apps, world, "REGISTRATION"), "REGISTRATION", token=s.token).json()["result"] == "CONFIRMED"
            assert list(events_of(engine, s, "REGISTRATION")[0]["flags"]) == flags
        finally:
            self._set_cutoff(engine, "NULL")

    def test_seating_records_no_seat_because_the_university_assigns_none(self, apps, world, engine):
        s = ready_student(engine, "SEATING", seat_no="A-7")  # even a leftover master seat is ignored
        card = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()["student"]
        assert field(card, "seat_no") is None
        assert not any("seat" in f["label"].lower() for f in card["fields"])
        confirm(operator(apps, world, "SEATING"), "SEATING", token=s.token)
        assert events_of(engine, s, "SEATING")[0]["details"] == {}

    def test_queue_confirmation_takes_the_next_position_in_the_same_transaction(self, apps, world, engine):
        first, second = ready_student(engine, "QUEUE"), ready_student(engine, "QUEUE")
        client = operator(apps, world, "QUEUE")
        confirm(client, "QUEUE", token=first.token)
        confirm(client, "QUEUE", token=second.token)
        pos = [q(engine, "SELECT queue_position, status FROM queue WHERE student_id = :s", s=x.id)[0] for x in (first, second)]
        assert pos[1]["queue_position"] == pos[0]["queue_position"] + 1 and pos[0]["status"] == "QUEUED"
        assert events_of(engine, first, "QUEUE")[0]["details"]["queue_position"] == pos[0]["queue_position"]

    def test_the_queue_card_shows_the_position_the_student_will_get(self, apps, world, engine):
        s = ready_student(engine, "QUEUE")
        card = scan(operator(apps, world, "QUEUE"), "QUEUE", s.token).json()["student"]
        assert "Position" in field(card, "queue_position")
        assert field(card, "sequence_no") is None, "the ceremony no longer has sequence numbers"

    def test_a_queue_scan_never_touches_the_led_or_the_display_snapshot(self, apps, world, engine):
        # Golden rule 9: only the Stage operator changes the public LED. The engine has no LED state at all.
        before = q(engine, "SELECT count(*) AS n FROM display_snapshot")[0]["n"]
        s = ready_student(engine, "QUEUE")
        confirm(operator(apps, world, "QUEUE"), "QUEUE", token=s.token)
        assert q(engine, "SELECT count(*) AS n FROM display_snapshot")[0]["n"] == before

    def test_the_return_card_shows_whether_a_thobe_was_issued_and_lunch_shows_eligibility(self, apps, world, engine):
        s = ready_student(engine, "THOBE_RETURN")
        card = scan(operator(apps, world, "THOBE_RETURN"), "THOBE_RETURN", s.token).json()["student"]
        assert field(card, "thobe_issued").startswith("Yes")
        lonely = make_student(engine)
        assert field(scan(operator(apps, world, "THOBE_RETURN"), "THOBE_RETURN", lonely.token).json()["student"], "thobe_issued") == "Not on record yet"
        lunch = ready_student(engine, "LUNCH")
        assert field(scan(operator(apps, world, "LUNCH"), "LUNCH", lunch.token).json()["student"], "eligibility").startswith("Thobe returned")


# --------------------------------------------------------------------------- #
# Plain messages on screen, technical detail in the log (golden rule 11)
# --------------------------------------------------------------------------- #
class TestMessagesAndLogs:
    def _scenarios(self, apps, world, engine):
        """(label, response json, technical marker that must appear ONLY in the log)."""
        out = []
        reg = operator(apps, world, "REGISTRATION")
        out.append(("unknown token", scan(reg, "REGISTRATION", "zz-bogus-" + uuid.uuid4().hex).json(), "invalid_token"))
        inactive = ready_student(engine, "REGISTRATION", active=False)
        out.append(("inactive", scan(reg, "REGISTRATION", inactive.token).json(), "student_inactive"))
        blocked = make_student(engine)
        out.append(("prerequisite", scan(operator(apps, world, "SEATING"), "SEATING", blocked.token).json(), "prerequisite:THOBE_ALLOCATION"))
        done = ready_student(engine, "REGISTRATION")
        confirm(reg, "REGISTRATION", token=done.token)
        out.append(("duplicate", scan(reg, "REGISTRATION", done.token).json(), "already_completed"))
        out.append(("unknown prn", reg.post("/search", json={"prn": "NOPE", "station_id": "REG-01"}).json(), "prn_not_found"))
        return out

    def test_every_rejection_is_one_plain_sentence(self, apps, world, engine):
        for label, body, _ in self._scenarios(apps, world, engine):
            assert body["result"] in {"INVALID", "REJECTED", "DUPLICATE"}, label
            assert_plain(body["message"])

    def test_the_technical_detail_is_in_the_log_and_not_on_the_screen(self, apps, world, engine, caplog):
        caplog.set_level(logging.DEBUG, logger="backend.engine")
        scenarios = self._scenarios(apps, world, engine)
        log_text = caplog.text
        for label, body, marker in scenarios:
            assert f"rule={marker}" in log_text, f"{label}: the log should say which rule fired"
            assert marker not in str(body), f"{label}: the rule name leaked to the operator"
        assert re.search(r"student_id=[0-9a-f-]{36}", log_text)  # detail the operator never sees
        for _, body, _ in scenarios:
            assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", str(body["message"]))

    def test_an_unexpected_failure_shows_a_calm_message_and_logs_the_stack_trace(self, apps, world, engine, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG, logger="backend.engine")
        secret = "internal-detail-that-must-not-reach-the-screen-7f3a"
        monkeypatch.setattr(service, "insert_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(secret)))
        s = ready_student(engine, "REGISTRATION")
        response = confirm(operator(apps, world, "REGISTRATION"), "REGISTRATION", token=s.token)
        assert response.status_code == 503
        message = response.json()["detail"]["message"]
        assert message == "One moment, please try again." and secret not in response.text
        assert_plain(message)
        assert secret in caplog.text and "Traceback" in caplog.text

    def test_a_persistent_failure_still_never_shows_technical_words(self, apps, world, engine, monkeypatch):
        monkeypatch.setattr(service, "insert_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("psycopg2 OperationalError 08006")))
        s = ready_student(engine, "REGISTRATION")
        for _ in range(3):
            assert_plain(confirm(operator(apps, world, "REGISTRATION"), "REGISTRATION", token=s.token).json()["detail"]["message"])


class TestEveryAttemptIsLogged:
    """TODO.md Phase 6: "Every attempt written to `scan_log`".

    Refusals and confirmations were written; a SUCCESSFUL scan was not. That is the one attempt the log
    most needs, because it is the only record that a student stood at that station and was shown to that
    operator: without it the log cannot answer "was this student ever presented here, and did the operator
    walk away without confirming?".
    """

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_a_ready_scan_writes_a_full_scan_log_row(self, apps, world, engine, activity):
        s = ready_student(engine, activity, seat_no="B-12")
        before = totals(engine)
        body = scan(operator(apps, world, activity), activity, s.token).json()
        assert body["result"] == "READY"

        rows = log_of(engine, s, activity)
        assert len(rows) == 1, "a successful scan is an attempt and must be in scan_log"
        [row] = rows
        assert row["result"] == "READY"
        assert row["venue_id"] == OWNER[activity]
        assert row["station_id"] == STATION[activity]
        assert row["activity"] == activity
        assert row["operator_id"] == world.op_ids[activity]
        assert row["student_id"] == s.id
        assert row["token_presented"] == s.token
        assert row["prn_entered"] is None
        assert row["message"] == body["message"]
        assert row["event_id"] is None                  # the operator has not confirmed: nothing was recorded
        assert row["occurred_at"] is not None
        assert row["details"]["stage"] == "scan"
        assert totals(engine) == before                 # ... and the preview still writes no activity at all

    def test_a_scan_that_is_never_confirmed_is_still_in_the_log(self, apps, world, engine):
        s = ready_student(engine, "SEATING", seat_no="C-04")
        scan(operator(apps, world, "SEATING"), "SEATING", s.token)
        assert events_of(engine, s, "SEATING") == []
        assert [r["result"] for r in log_of(engine, s, "SEATING")] == ["READY"]

    def test_the_same_qr_scanned_twice_before_confirming_is_logged_twice(self, apps, world, engine):
        s = ready_student(engine, "REGISTRATION")
        client = operator(apps, world, "REGISTRATION")
        scan(client, "REGISTRATION", s.token)
        scan(client, "REGISTRATION", s.token)
        assert [r["result"] for r in log_of(engine, s, "REGISTRATION")] == ["READY", "READY"]

    def test_a_ready_manual_search_is_logged_with_the_prn_it_was_given(self, apps, world, engine):
        s = ready_student(engine, "REGISTRATION")
        client = operator(apps, world, "REGISTRATION")
        body = client.post("/search", json={"prn": s.prn, "station_id": "REG-01"}).json()
        assert body["result"] == "READY" and body["manual"] is True

        [row] = log_of(engine, s, "REGISTRATION")
        assert row["result"] == "READY" and row["prn_entered"] == s.prn and row["token_presented"] is None
        assert row["student_id"] == s.id and row["event_id"] is None
        assert row["details"]["stage"] == "search"

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_no_attempt_at_any_station_goes_unrecorded(self, apps, world, engine, activity):
        """Four attempts by the same student: scan, scan again, confirm, scan once more. Four rows."""
        s = ready_student(engine, activity, seat_no="A-01")
        client = operator(apps, world, activity)
        scan(client, activity, s.token)
        scan(client, activity, s.token)
        confirm(client, activity, token=s.token)
        scan(client, activity, s.token)
        assert [r["result"] for r in log_of(engine, s, activity)] == ["READY", "READY", "SUCCESS", "DUPLICATE"]


class TestLatency:
    def test_scan_and_confirm_take_well_under_200ms_server_side(self, apps, world, engine):
        client = operator(apps, world, "REGISTRATION")
        warm = make_student(engine)  # first request pays for imports and the connection pool
        scan(client, "REGISTRATION", warm.token)
        scan_ms, confirm_ms = [], []
        for _ in range(20):
            s = make_student(engine)
            r1 = scan(client, "REGISTRATION", s.token)
            r2 = confirm(client, "REGISTRATION", token=s.token)
            assert r1.json()["result"] == "READY" and r2.json()["result"] == "CONFIRMED"
            scan_ms.append(float(r1.headers["x-process-time-ms"]))
            confirm_ms.append(float(r2.headers["x-process-time-ms"]))
        median = lambda xs: sorted(xs)[len(xs) // 2]  # noqa: E731
        print(f"\nserver-side ms over 20 pairs  scan: median {median(scan_ms):.1f} max {max(scan_ms):.1f}"
              f"  |  confirm: median {median(confirm_ms):.1f} max {max(confirm_ms):.1f}")
        assert max(scan_ms) < 200, scan_ms
        assert max(confirm_ms) < 200, confirm_ms
        assert sorted(confirm_ms)[len(confirm_ms) // 2] < 100  # the median is far under that

    def test_the_response_also_reports_its_own_processing_time(self, apps, world, engine):
        body = scan(operator(apps, world, "REGISTRATION"), "REGISTRATION", make_student(engine).token).json()
        assert 0 <= body["elapsed_ms"] < 200


# --------------------------------------------------------------------------- #
# Operator screen
# --------------------------------------------------------------------------- #
class TestStationScreen:
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_the_screen_has_an_autofocused_scan_box_and_the_configured_button(self, apps, world, activity):
        page = operator(apps, world, activity).get(f"/station/{slug(activity)}")
        assert page.status_code == 200
        if activity == "STAGE":  # Phase 11: the Stage operator runs the Stage Controller, not a scan box
            assert 'id="stage-root"' in page.text and CONFIRM_LABEL["STAGE"] in page.text
            return
        assert 'id="scan"' in page.text and "autofocus" in page.text
        assert CONFIRM_LABEL[activity] in page.text
        assert f'data-station-id="{STATION[activity]}"' in page.text
        assert "/static/station_logic.js" in page.text and "/static/station.js" in page.text

    def test_an_admin_picks_which_station_to_act_as(self, apps, world):
        page = admin(apps, "college").get("/station/registration")
        assert page.status_code == 200 and "REG-01" in page.text and "REG-02" in page.text

    def test_the_scripts_and_styles_are_served_locally_with_no_cdn(self, apps, world):
        client = operator(apps, world, "REGISTRATION")
        for path in ("/static/station_logic.js", "/static/station.js", "/static/app.css"):
            assert client.get(path).status_code == 200
        page = client.get("/station/registration").text
        assert "http://" not in page and "https://" not in page  # offline-first: nothing fetched from the internet

    def test_the_screen_wires_focus_debounce_colour_and_sound(self):
        source = (REPO_ROOT / "static" / "station.js").read_text(encoding="utf-8")
        for needle in ("focus()", "stripScannerSuffix", "createDebouncer", "classifyResult", "AudioContext"):
            assert needle in source, needle


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_operator_screen_logic_under_node():
    """Scanner-suffix stripping, debounce, focus, colour and sound are exercised for real under node
    (tests/js/station.test.js) with a fake DOM, since there is no browser in the test run."""
    files = sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "tests" / "js").glob("*.test.js"))
    assert files, "no JS tests found"
    run = subprocess.run(["node", "--test", *files], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr

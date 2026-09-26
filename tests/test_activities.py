"""Phases 7-12 bundle: the guarantees that matter for Robe Allocation, Seating, Queue, Robe Return, Lunch.

All five are CONFIGURATION on the Phase 6 engine (backend/engine/activities.py); nothing here tests a
separate pipeline. Reporting and Stage keep their Phase 6 tests.

HOW THESE TESTS ARE RUN: every test below uses ONE shared PostgreSQL test database with a single app
instance. In this single-server, role-based model, each operator's role determines which activity they
can perform, and every prerequisite check is an immediate local check against the database.
"""
import json

import pytest
from sqlalchemy import text

from backend import users as users_svc
from tests.test_auth import ACTIVITIES, PASSWORD, api_login, new_client
from tests.test_schema import STATUS_AFTER_STEP, _run_threads
from tests.test_station_engine import (  # noqa: F401  (engine/world/apps are pytest fixtures)
    _CLIENTS,
    apps,
    clock,
    confirm,
    engine,
    events_of,
    field,
    log_of,
    make_student,
    operator,
    q,
    ready_student,
    scan,
    seed_events,
    totals,
    world,
)

EXTRA_OPS = [("QUE-02", "QUEUE"), ("QUE-03", "QUEUE"), ("LUN-02", "LUNCH")]


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()  # clients cached by the engine tests belong to a database that no longer exists
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module")
def tokens(engine, world, apps):
    """Clients and activities for each operator used here: the default ones plus extra Queue and Lunch."""
    with engine.begin() as c:
        for sid, activity in EXTRA_OPS:
            users_svc.create_user(c, username=f"act-{sid.lower()}", password=PASSWORD, role=activity)
    out = {}
    for sid, activity in EXTRA_OPS:
        client = new_client(apps)
        assert api_login(client, f"act-{sid.lower()}").status_code == 200
        out[sid] = (client, activity)
    for activity in ("THOBE_ALLOCATION", "SEATING", "QUEUE", "THOBE_RETURN", "LUNCH"):
        out[f"{activity[:3]}-01"] = (operator(apps, world, activity), activity)
    return out


def call(apps, tokens, path, station_id, **body):
    """One request as the operator of `station_id` (own client, own session), as concurrent stations would."""
    client, activity = tokens[station_id]
    return client.post(path, json={"activity": activity, **body}).json()


def seed_at(engine, student, activity, *, hours_ago=2, details=None, kind="COMPLETE", corrects=None, cycle=1, station=None):
    """An earlier record, backdated, so 'shows the EARLIER time' is distinguishable from 'shows now'."""
    if details is None:
        details = {} if kind == "COMPLETE" else {"reason": "seeded"}
    with engine.begin() as c:
        return c.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, "
                 "completion_cycle, corrects_event_id, server_time) VALUES (:s, :a, :k, gen_random_uuid(), "
                 "CAST(:f AS text[]), CAST(:d AS jsonb), :c, :x, now() - make_interval(hours => :h)) "
                 "RETURNING event_id, server_time"),
            {"s": student.id, "a": activity, "k": kind,
             "f": ["CORRECTED"] if kind == "WAIVER" else [], "d": json.dumps(details),
             "c": cycle, "x": corrects, "h": hours_ago},
        ).one()


def fingerprint(engine, sql):
    return q(engine, sql)[0]["f"]


# =========================================================================== ROBE ALLOCATION
class TestThobeAllocation:
    def test_confirms_once_and_the_duplicate_shows_the_earlier_time(self, apps, world, engine):
        s = ready_student(engine, "THOBE_ALLOCATION")
        earlier = seed_at(engine, s, "THOBE_ALLOCATION", hours_ago=3, station="THO-09")
        client = operator(apps, world, "THOBE_ALLOCATION")
        body = scan(client, "THOBE_ALLOCATION", s.token).json()
        assert body["result"] == "DUPLICATE" and body["colour"] == "amber"
        assert body["message"] == f"ROBE ALREADY ALLOCATED — {clock(earlier.server_time)}"  # the earlier time, not now
        assert body["earlier"]["time"] == clock(earlier.server_time)
        assert confirm(client, "THOBE_ALLOCATION", token=s.token).json()["result"] == "DUPLICATE"
        assert len(events_of(engine, s, "THOBE_ALLOCATION")) == 1

        fresh = ready_student(engine, "THOBE_ALLOCATION")
        assert confirm(client, "THOBE_ALLOCATION", token=fresh.token).json()["result"] == "CONFIRMED"
        assert scan(client, "THOBE_ALLOCATION", fresh.token).json()["result"] == "DUPLICATE"
        assert len(events_of(engine, fresh, "THOBE_ALLOCATION")) == 1

    def test_allocation_is_a_plain_confirmation_no_number_no_size(self, apps, world, engine):
        s = ready_student(engine, "THOBE_ALLOCATION")
        client = operator(apps, world, "THOBE_ALLOCATION")
        done = client.post("/confirm", json={"token": s.token, "activity": "THOBE_ALLOCATION", "thobe_no": "T-77", "size": "XL"}).json()
        assert done["result"] == "CONFIRMED"
        assert events_of(engine, s, "THOBE_ALLOCATION")[0]["details"] == {}  # SYSTEM_SPEC C2: identical, unnumbered

    def test_allocation_does_not_alter_the_student_record_or_qr_for_later_scans(self, apps, world, engine):
        s = make_student(engine, seat_no="B-4")
        seed_events(engine, s, ["REGISTRATION"])
        row_sql = f"SELECT md5(r::text) AS f FROM students r WHERE id = '{s.id}'"
        tok_sql = f"SELECT md5(string_agg(t::text, '|' ORDER BY id)) AS f FROM qr_tokens t WHERE student_id = '{s.id}'"
        before = (fingerprint(engine, row_sql), fingerprint(engine, tok_sql))
        assert confirm(operator(apps, world, "THOBE_ALLOCATION"), "THOBE_ALLOCATION", token=s.token).json()["result"] == "CONFIRMED"
        assert (fingerprint(engine, row_sql), fingerprint(engine, tok_sql)) == before  # not one byte changed
        later = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()  # the same QR still works downstream
        assert later["result"] == "READY" and field(later["student"], "prn") == s.prn


# =========================================================================== SEATING
class TestSeating:
    """The university assigns no seats, so Seating is a plain "this student is seated" checkpoint,
    exactly like Robe Allocation: no seat is shown, none is asked for, and none is recorded."""

    def test_no_seat_is_shown_and_a_client_supplied_seat_is_ignored(self, apps, world, engine):
        s = ready_student(engine, "SEATING", seat_no="A-12")   # a leftover master seat changes nothing
        client = operator(apps, world, "SEATING")
        smuggled = {"seat_no": "Z-99", "seat": "Z-99", "details": {"seat_no": "Z-99"}}
        card = client.post("/scan", json={"token": s.token, "activity": "SEATING", **smuggled}).json()["student"]
        assert field(card, "seat_no") is None
        assert not any("seat" in f["label"].lower() for f in card["fields"])
        done = client.post("/confirm", json={"token": s.token, "activity": "SEATING", **smuggled}).json()
        assert done["result"] == "CONFIRMED"
        assert events_of(engine, s, "SEATING")[0]["details"] == {}   # nothing smuggled onto the event
        assert q(engine, "SELECT seat_no FROM students WHERE id = :s", s=s.id)[0]["seat_no"] == "A-12"  # master untouched

    def test_blocked_without_thobe_allocation_with_the_specified_message(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION"])  # registered, but no robe yet
        client = operator(apps, world, "SEATING")
        before = totals(engine)
        for body in (scan(client, "SEATING", s.token).json(), confirm(client, "SEATING", token=s.token).json()):
            assert body["result"] == "REJECTED" and body["message"] == "SEATING NOT AVAILABLE — ROBE NOT RECEIVED"
        assert totals(engine) == before and events_of(engine, s, "SEATING") == []

    def test_a_second_scan_names_the_time_it_was_confirmed_and_no_seat(self, apps, world, engine):
        s = ready_student(engine, "SEATING", seat_no="A-1")
        earlier = seed_at(engine, s, "SEATING", hours_ago=2)
        body = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()
        assert body["result"] == "DUPLICATE"
        assert body["message"] == f"SEATING ALREADY CONFIRMED — {clock(earlier.server_time)}"
        assert len(events_of(engine, s, "SEATING")) == 1

    def test_a_student_with_no_seat_on_the_master_list_is_seated_exactly_like_everyone_else(self, apps, world, engine):
        s = ready_student(engine, "SEATING", seat_no=None)
        card = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()["student"]
        assert [f["key"] for f in card["fields"]] == ["prn", "programme", "school"]
        assert confirm(operator(apps, world, "SEATING"), "SEATING", token=s.token).json()["result"] == "CONFIRMED"
        assert events_of(engine, s, "SEATING")[0]["details"] == {}


# =========================================================================== QUEUE
class TestQueue:
    def _queue_rows(self, engine, students):
        ids = [s.id for s in students]
        return q(engine, "SELECT qu.student_id, qu.queue_position, e.details, e.server_time FROM queue qu "
                         "JOIN activity_events e ON e.student_id = qu.student_id AND e.activity = 'QUEUE' AND e.kind = 'COMPLETE' "
                         "WHERE qu.student_id = ANY(:ids) ORDER BY qu.queue_position", ids=ids)

    def test_blocked_without_a_robe(self, apps, world, engine):
        # Redesign: Seating is optional, so the Queue's hard block is the robe, not the seat.
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION"])  # reported, no robe
        client = operator(apps, world, "QUEUE")
        before = totals(engine)
        for body in (scan(client, "QUEUE", s.token).json(), confirm(client, "QUEUE", token=s.token).json()):
            assert body["result"] == "REJECTED" and body["message"] == "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"
        assert totals(engine) == before
        assert q(engine, "SELECT count(*) AS n FROM queue WHERE student_id = :s", s=s.id)[0]["n"] == 0

    def test_not_blocked_without_seating(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION"])  # robe, never seated
        client = operator(apps, world, "QUEUE")
        assert confirm(client, "QUEUE", token=s.token).json()["result"] == "CONFIRMED"
        assert q(engine, "SELECT count(*) AS n FROM queue WHERE student_id = :s", s=s.id)[0]["n"] == 1

    def test_positions_follow_confirmation_order_and_nothing_else(self, apps, world, engine):
        students = [ready_student(engine, "QUEUE") for _ in range(4)]  # created in one order...
        client = operator(apps, world, "QUEUE")
        for s in reversed(students):                                   # ...confirmed in the opposite one
            assert confirm(client, "QUEUE", token=s.token).json()["result"] == "CONFIRMED"
        rows = {r["student_id"]: r["queue_position"] for r in self._queue_rows(engine, students)}
        positions = [rows[s.id] for s in reversed(students)]
        assert positions == sorted(positions) and positions == list(range(positions[0], positions[0] + 4))
        shown = scan(client, "QUEUE", ready_student(engine, "QUEUE").token).json()["student"]
        assert "Position" in field(shown, "queue_position")
        assert field(shown, "sequence_no") is None, "there is no convocation sequence number any more"

    @pytest.mark.parametrize("with_noise", [False, True], ids=["queue-only", "with-other-stadium-traffic"])
    def test_concurrent_confirms_from_three_queue_stations_get_strict_confirmation_order(self, apps, world, engine, tokens, with_noise):
        """The one place a race is likely. Three Queue stations confirm 12 students at once, three rounds
        running; positions must be unique, gap-free, and in exactly the order the events were committed."""
        stations = ["QUE-01", "QUE-02", "QUE-03"]
        for _round in range(3):
            students = [ready_student(engine, "QUEUE") for _ in range(12)]
            noise = [ready_student(engine, "SEATING") for _ in range(6)] if with_noise else []
            jobs = [(stations[i % 3], students[i]) for i in range(12)]

            def work(i):
                if i < 12:
                    station, student = jobs[i]
                    return call(apps, tokens, "/confirm", station, token=student.token)["result"]
                return call(apps, tokens, "/confirm", "SEA-01", token=noise[i - 12].token)["result"]

            results = _run_threads(work, 12 + len(noise))
            assert not [r for r in results if isinstance(r, Exception)], results
            assert results == ["CONFIRMED"] * (12 + len(noise))

            rows = self._queue_rows(engine, students)
            assert len(rows) == 12
            positions = [r["queue_position"] for r in rows]
            assert positions == list(range(positions[0], positions[0] + 12)), positions      # unique and gap-free
            assert [r["details"]["queue_position"] for r in rows] == positions               # what the event recorded

        table = [r["queue_position"] for r in q(engine, "SELECT queue_position FROM queue ORDER BY queue_position")]
        assert table == list(range(1, len(table) + 1))  # the whole queue is 1..N: no gaps, no repeats, ever

    def test_the_same_student_confirmed_at_two_queue_stations_at_once_is_queued_once(self, apps, world, engine, tokens):
        s = ready_student(engine, "QUEUE")
        others = [ready_student(engine, "QUEUE") for _ in range(4)]
        jobs = [("QUE-01", s), ("QUE-02", s), ("QUE-03", s), ("QUE-01", s)] + [("QUE-02", o) for o in others]
        results = _run_threads(lambda i: call(apps, tokens, "/confirm", jobs[i][0], token=jobs[i][1].token)["result"], len(jobs))
        assert sorted(results[:4]) == ["CONFIRMED", "DUPLICATE", "DUPLICATE", "DUPLICATE"] and results[4:] == ["CONFIRMED"] * 4
        assert q(engine, "SELECT count(*) AS n FROM queue WHERE student_id = :s", s=s.id)[0]["n"] == 1
        table = [r["queue_position"] for r in q(engine, "SELECT queue_position FROM queue ORDER BY queue_position")]
        assert table == list(range(1, len(table) + 1))  # the losers burned no position numbers

    def test_a_queue_confirmation_never_modifies_the_display_snapshot_or_any_led_state(self, apps, world, engine):
        students = [ready_student(engine, "QUEUE") for _ in range(3)]
        with engine.begin() as c:
            for s in students:
                c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school, award) "
                               "VALUES (:s, :n, 'B.Tech', 'Engineering', 'Gold medal')"), {"s": s.id, "n": s.name})
        snap = "SELECT md5(coalesce(string_agg(d::text || d.xmin::text, '|' ORDER BY d.student_id), '')) AS f FROM display_snapshot d"
        state = "SELECT count(*) AS f FROM queue WHERE status <> 'QUEUED'"  # DISPLAYED / HELD / DONE are what the LED follows
        before = (fingerprint(engine, snap), fingerprint(engine, state))
        client = operator(apps, world, "QUEUE")
        replies = [confirm(client, "QUEUE", token=s.token).json() for s in students]  # confirms
        replies += [scan(client, "QUEUE", s.token).json() for s in students]          # duplicates
        replies += [scan(client, "QUEUE", make_student(engine).token).json()]         # a rejection
        assert (fingerprint(engine, snap), fingerprint(engine, state)) == before      # xmin included: not even rewritten
        assert q(engine, "SELECT count(*) AS n FROM queue WHERE status = 'DISPLAYED'")[0]["n"] == 0
        assert not any("led" in key.lower() or "snapshot" in key.lower() for r in replies for key in r)  # no LED-shaped field

    def test_after_an_admin_reversal_the_student_is_queued_again_at_the_back(self, apps, world, engine):
        first = ready_student(engine, "QUEUE")
        client = operator(apps, world, "QUEUE")
        confirm(client, "QUEUE", token=first.token)
        original = events_of(engine, first, "QUEUE")[0]
        old_position = q(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=first.id)[0]["queue_position"]
        seed_at(engine, first, "QUEUE", kind="REVERSAL", corrects=original["event_id"], hours_ago=0)  # Admin reversed it
        other = ready_student(engine, "QUEUE")
        confirm(client, "QUEUE", token=other.token)
        again = confirm(client, "QUEUE", token=first.token).json()
        assert again["result"] == "CONFIRMED"
        rows = q(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=first.id)
        assert len(rows) == 1 and rows[0]["queue_position"] > old_position
        other_pos = q(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=other.id)[0]["queue_position"]
        assert rows[0]["queue_position"] > other_pos  # strictly behind everyone who confirmed before the re-queue


# =========================================================================== ROBE RETURN
class TestThobeReturn:
    def test_confirms_once_and_the_duplicate_shows_the_earlier_time(self, apps, world, engine):
        s = ready_student(engine, "THOBE_RETURN")
        client = operator(apps, world, "THOBE_RETURN")
        assert confirm(client, "THOBE_RETURN", token=s.token).json()["result"] == "CONFIRMED"
        older = ready_student(engine, "THOBE_RETURN")
        earlier = seed_at(engine, older, "THOBE_RETURN", hours_ago=4, station="RET-09")
        body = scan(client, "THOBE_RETURN", older.token).json()
        assert body["result"] == "DUPLICATE" and body["message"] == f"ALREADY RETURNED — {clock(earlier.server_time)}"
        assert confirm(client, "THOBE_RETURN", token=s.token).json()["result"] == "DUPLICATE"
        assert len(events_of(engine, s, "THOBE_RETURN")) == 1

    def test_it_is_configured_to_require_queue_and_the_thobe_allocation(self, apps, world, engine):
        client = operator(apps, world, "THOBE_RETURN")
        nothing = make_student(engine)
        assert scan(client, "THOBE_RETURN", nothing.token).json()["message"] == "ROBE RETURN NOT AVAILABLE — QUEUE PENDING"

        allocated_but_not_queued = make_student(engine)
        seed_at(engine, allocated_but_not_queued, "THOBE_ALLOCATION", hours_ago=1)
        assert scan(client, "THOBE_RETURN", allocated_but_not_queued.token).json()["message"] == "ROBE RETURN NOT AVAILABLE — QUEUE PENDING"

        no_thobe = make_student(engine)
        seed_events(engine, no_thobe, ["REGISTRATION"])
        seed_at(engine, no_thobe, "QUEUE", hours_ago=1)
        body = scan(client, "THOBE_RETURN", no_thobe.token).json()
        assert body["result"] == "REJECTED" and body["message"] == "ROBE RETURN NOT AVAILABLE — NO ROBE WAS ISSUED"
        assert confirm(client, "THOBE_RETURN", token=no_thobe.token).json()["result"] == "REJECTED"
        assert events_of(engine, no_thobe, "THOBE_RETURN") == []

        both = ready_student(engine, "THOBE_RETURN")
        assert scan(client, "THOBE_RETURN", both.token).json()["result"] == "READY"

    def test_the_card_confirms_a_thobe_was_issued(self, apps, world, engine):
        s = ready_student(engine, "THOBE_RETURN")
        card = scan(operator(apps, world, "THOBE_RETURN"), "THOBE_RETURN", s.token).json()["student"]
        assert field(card, "thobe_issued").startswith("Yes — issued ")


# =========================================================================== LUNCH
class TestLunch:
    def test_blocked_without_a_return_or_waiver_and_allowed_with_either(self, apps, world, engine):
        client = operator(apps, world, "LUNCH")
        none = make_student(engine)
        seed_events(engine, none, ACTIVITIES[:ACTIVITIES.index("THOBE_RETURN")])  # everything through Stage; nothing returned
        before = totals(engine)
        for body in (scan(client, "LUNCH", none.token).json(), confirm(client, "LUNCH", token=none.token).json()):
            assert body["result"] == "REJECTED" and body["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"
        assert totals(engine) == before
        returned = ready_student(engine, "LUNCH")
        assert scan(client, "LUNCH", returned.token).json()["result"] == "READY"
        waived = make_student(engine)
        seed_events(engine, waived, ACTIVITIES[:ACTIVITIES.index("THOBE_RETURN")])
        seed_at(engine, waived, "THOBE_RETURN", kind="WAIVER", hours_ago=1)  # an EXISTING Admin waiver record
        ready = scan(client, "LUNCH", waived.token).json()
        assert ready["result"] == "READY" and field(ready["student"], "eligibility") == "Robe waived by Admin"
        assert confirm(client, "LUNCH", token=waived.token).json()["result"] == "CONFIRMED"

    def test_a_reversed_waiver_no_longer_unlocks_lunch(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ACTIVITIES[:ACTIVITIES.index("THOBE_RETURN")])
        waiver = seed_at(engine, s, "THOBE_RETURN", kind="WAIVER", hours_ago=2)
        assert scan(operator(apps, world, "LUNCH"), "LUNCH", s.token).json()["result"] == "READY"
        seed_at(engine, s, "THOBE_RETURN", kind="REVERSAL", corrects=waiver.event_id, hours_ago=1)
        body = scan(operator(apps, world, "LUNCH"), "LUNCH", s.token).json()
        assert body["result"] == "REJECTED" and body["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"

    def test_the_duplicate_shows_the_earlier_claim_time(self, apps, world, engine):
        s = ready_student(engine, "LUNCH")
        earlier = seed_at(engine, s, "LUNCH", hours_ago=2, station="LUN-07")
        body = scan(operator(apps, world, "LUNCH"), "LUNCH", s.token).json()
        assert body["result"] == "DUPLICATE" and body["message"] == f"LUNCH ALREADY CLAIMED — {clock(earlier.server_time)}"
        assert body["earlier"]["time"] == clock(earlier.server_time) and len(events_of(engine, s, "LUNCH")) == 1

    def test_two_lunch_counters_confirming_the_same_student_at_once_leave_exactly_one_row(self, apps, world, engine, tokens):
        for _ in range(3):
            s = ready_student(engine, "LUNCH")
            stations = ["LUN-01", "LUN-02"] * 4
            results = _run_threads(lambda i: call(apps, tokens, "/confirm", stations[i], token=s.token)["result"], 8)
            assert sorted(results) == ["CONFIRMED"] + ["DUPLICATE"] * 7
            event = events_of(engine, s, "LUNCH")
            assert len(event) == 1
            assert sorted(r["result"] for r in log_of(engine, s, "LUNCH")) == ["DUPLICATE"] * 7 + ["SUCCESS"]


# =========================================================================== THE WHOLE JOURNEY
class TestFullJourney:
    def _walk(self, apps, world, engine, waive_return):
        s = make_student(engine, seat_no="C-3")
        seen = []
        for activity in ACTIVITIES:
            if activity == "THOBE_RETURN" and waive_return:
                seed_at(engine, s, "THOBE_RETURN", kind="WAIVER", hours_ago=0)  # Admin waiver instead of a return
            else:
                client = operator(apps, world, activity)
                assert scan(client, activity, s.token).json()["result"] == "READY", activity
                assert confirm(client, activity, token=s.token).json()["result"] == "CONFIRMED", activity
            seen.append(q(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id)[0]["status"])
        return s, seen

    def test_registration_to_lunch_ends_exited(self, apps, world, engine):
        s, seen = self._walk(apps, world, engine, waive_return=False)
        assert seen[-1] == "EXITED"
        assert len(events_of(engine, s)) == len(ACTIVITIES) == 6
        # Each of the six steps: the scan the operator was shown, then the confirm. No false duplicate anywhere.
        assert [r["result"] for r in log_of(engine, s)] == ["READY", "SUCCESS"] * 6

    def test_the_same_journey_with_an_admin_waived_return_also_ends_exited(self, apps, world, engine):
        s, seen = self._walk(apps, world, engine, waive_return=True)
        assert seen[-1] == "EXITED"
        kinds = {e["activity"]: e["kind"] for e in events_of(engine, s) if e["activity"] in ("THOBE_RETURN", "LUNCH")}
        assert kinds == {"THOBE_RETURN": "WAIVER", "LUNCH": "COMPLETE"}

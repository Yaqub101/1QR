"""Phases 13 + 16: Admin corrections, the waiver, exceptions, audit, and who may reach any of it.

THE PROPERTY UNDER TEST: a correction is a NEW row that references the original; history is never mutated.
"Never mutated" is checked at the strongest level available from outside the database: for the original row we
compare its content hash AND its physical tuple identity (xmin, ctid). Any UPDATE, even one that changes no
value, writes a new tuple version and moves xmin/ctid, so an identical (xmin, ctid, content) triple means the
row was not touched at all. We also fingerprint the whole activity_events table minus the one new row.
"""
import pathlib
import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.admin import corrections
from tests.admin_support import add_event, add_student, error_code, parse_csv, physical_row, rows, scalar, table_fingerprint
from tests.test_auth import ACTIVITIES, PASSWORD, RESTRICT_VIOLATION, api_login, new_client
from tests.test_schema import _run_threads
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    make_student,
    operator,
    scan,
    world,
)

OPERATOR_ROLES = list(ACTIVITIES)
REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module", autouse=True)
def _admins_signed_in(apps, _fresh_client_cache):
    """Sign the Admin in at every server once, up front: signing in writes audit rows, and the tests below compare
    audit counts before and after a refused request."""
    for venue in ("college", "stadium", "hall", "central"):
        admin(apps, venue)


def reverse(apps, venue, event_id, reason="entered for the wrong student", client=None):
    body = {"event_id": str(event_id)}
    if reason is not None:
        body["reason"] = reason
    return (client or admin(apps, venue)).post("/admin/api/corrections/reverse", json=body)


def waive(apps, venue, student, reason="robe lost on the way", client=None):
    body = {"student_id": str(student.id if hasattr(student, "id") else student)}
    if reason is not None:
        body["reason"] = reason
    return (client or admin(apps, venue)).post("/admin/api/corrections/waive-return", json=body)


def journey_upto(engine, activity, **kw):
    """A student with every activity BEFORE `activity` completed, and the ids of those events."""
    s = add_student(engine, school="School of Law", **kw)
    ids = {a: add_event(engine, s, a) for a in ACTIVITIES[:ACTIVITIES.index(activity)]}
    return s, ids


def counts(engine):
    return {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in ("activity_events", "audit_log", "exceptions")}


# =========================================================================== THE CORRECTION ENDPOINT
class TestReversalNeverMutatesHistory:
    def test_a_reversal_is_a_new_row_and_the_original_is_byte_identical(self, apps, engine, world):
        s, ids = journey_upto(engine, "LUNCH")
        original = ids["SEATING"]
        before_row = physical_row(engine, original)
        before_others = table_fingerprint(engine, "activity_events", "event_id", f"event_id <> '{original}'")  # every row but the original
        events_before = scalar(engine, "SELECT count(*) FROM activity_events")

        r = reverse(apps, "stadium", original, "seated in the wrong seat")
        assert r.status_code == 200, r.text
        new_id = r.json()["correction_event_id"]

        assert physical_row(engine, original) == before_row                       # not one byte, not even a new tuple version
        assert scalar(engine, "SELECT count(*) FROM activity_events") == events_before + 1  # exactly ONE new row, nothing removed
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE event_id = :e", e=original) == 1
        # every OTHER row except the new one is also untouched
        # every OTHER row is untouched too: the table minus the original and the new row hashes exactly as before
        assert table_fingerprint(engine, "activity_events", "event_id", f"event_id <> '{original}' AND event_id <> '{new_id}'") == before_others

        new = rows(engine, "SELECT * FROM activity_events WHERE event_id = :e", e=new_id)[0]
        assert new["kind"] == "REVERSAL" and str(new["corrects_event_id"]) == str(original)          # references the original
        assert new["student_id"] == s.id and new["activity"] == "SEATING" and new["completion_cycle"] == 1
        assert new["operator_id"] == world.admin_id                                                   # who: the Admin
        assert new["details"]["reason"] == "seated in the wrong seat" and "CORRECTED" in new["flags"]  # why, flagged
        orig = rows(engine, "SELECT kind, flags FROM activity_events WHERE event_id = :e", e=original)[0]
        assert orig["kind"] == "COMPLETE" and "CORRECTED" not in orig["flags"]  # the original does NOT carry the flag: it was never edited

    def test_the_correction_is_audited_in_the_same_commit(self, apps, engine, world):
        s, ids = journey_upto(engine, "QUEUE")
        r = reverse(apps, "stadium", ids["SEATING"], "wrong seat").json()
        new = r["correction_event_id"]
        a = rows(engine, "SELECT * FROM audit_log WHERE event_id = :e", e=new)
        assert len(a) == 1 and a[0]["action"] == "ADMIN_REVERSAL" and a[0]["reason"] == "wrong seat"
        assert str(a[0]["corrects_event_id"]) == str(ids["SEATING"]) and a[0]["corrected_by"] == world.admin_id
        assert a[0]["student_id"] == s.id and a[0]["activity"] == "SEATING" and "CORRECTED" in a[0]["flags"]

    def test_the_derived_status_changes_because_a_row_was_added_not_because_one_was_edited(self, apps, engine):
        s, ids = journey_upto(engine, "THOBE_RETURN")               # Reporting .. Stage done
        status = lambda: scalar(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id)  # noqa: E731
        assert status() == "ROBE NOT RETURNED"
        assert reverse(apps, "stadium", ids["STAGE"], "pressed COMPLETE by accident").status_code == 200
        assert status() == "DEGREE NOT RECEIVED"                    # status went BACK
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s", s=s.id) == len(ids) + 1  # originals + 1 reversal

    def test_reason_is_mandatory_and_checked_on_the_server(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        before = counts(engine)
        for bad in (None, "", "   ", "\n\t  "):
            r = reverse(apps, "stadium", ids["SEATING"], bad)
            assert r.status_code == 400 and error_code(r) == "REASON_REQUIRED", repr(bad)
        assert reverse(apps, "stadium", ids["SEATING"], "x" * 501).status_code == 400
        assert counts(engine) == before  # nothing written by any refused attempt
        same = admin(apps, "stadium").post("/admin/api/corrections/reverse", json={"event_id": str(ids["SEATING"]), "reason": " a reason "})
        assert same.status_code == 200
        assert scalar(engine, "SELECT details->>'reason' FROM activity_events WHERE event_id = :e", e=same.json()["correction_event_id"]) == "a reason"

    @pytest.mark.parametrize("role", OPERATOR_ROLES)
    def test_an_operator_gets_403_and_nothing_is_written(self, apps, engine, world, role):
        s, ids = journey_upto(engine, "QUEUE")
        client = operator(apps, world, role)  # (signing in writes audit rows, so do it before taking the "before" snapshot)
        before, original = counts(engine), physical_row(engine, ids["SEATING"])
        for venue in ("college", "stadium", "hall"):
            other = new_client(apps[venue])
            other.headers["Authorization"] = f"Bearer {client.cookies.get('session')}"
            r = reverse(apps, venue, ids["SEATING"], client=other)
            assert r.status_code == 403 and error_code(r) == "FORBIDDEN"
        assert counts(engine) == before and physical_row(engine, ids["SEATING"]) == original

    def test_a_signed_out_caller_gets_401(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        anonymous = new_client(apps["stadium"])
        assert reverse(apps, "stadium", ids["SEATING"], client=anonymous).status_code == 401
        assert waive(apps, "hall", s, client=new_client(apps["hall"])).status_code == 401

    def test_the_service_refuses_a_non_admin_even_if_a_route_forgot_to(self, engine, world, apps):
        """Defence in depth: the guard lives in the service, not only on the route."""
        from types import SimpleNamespace
        s, ids = journey_upto(engine, "QUEUE")
        operator_principal = SimpleNamespace(role="SEATING", user_id=world.op_ids["SEATING"])
        with pytest.raises(corrections.CorrectionError) as exc:
            corrections.reverse_event(engine, principal=operator_principal,
                                      event_id=ids["SEATING"], reason="because")
        assert exc.value.status_code == 403
        with pytest.raises(corrections.CorrectionError) as exc:
            corrections.waive_return(engine, principal=operator_principal, student_id=s.id, reason="x")
        assert exc.value.status_code == 403

    def test_a_deputy_admin_has_identical_powers_and_is_named_in_the_trail(self, apps, engine, world):
        from backend import users as users_svc
        with engine.begin() as c:
            deputy_id = users_svc.create_user(c, username="deputy-1", password=PASSWORD, role="DEPUTY_ADMIN")
        client = new_client(apps["stadium"])
        assert api_login(client, "deputy-1").status_code == 200
        s, ids = journey_upto(engine, "QUEUE")
        r = reverse(apps, "stadium", ids["SEATING"], "deputy correction", client=client)
        assert r.status_code == 200
        assert scalar(engine, "SELECT operator_id FROM activity_events WHERE event_id = :e", e=r.json()["correction_event_id"]) == deputy_id
        assert scalar(engine, "SELECT corrected_by FROM audit_log WHERE event_id = :e", e=r.json()["correction_event_id"]) == deputy_id

    def test_only_completions_and_waivers_can_be_reversed_and_only_once(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        first = reverse(apps, "stadium", ids["SEATING"])
        assert first.status_code == 200
        again = reverse(apps, "stadium", ids["SEATING"])
        assert again.status_code == 409 and error_code(again) == "ALREADY_REVERSED"
        assert reverse(apps, "stadium", first.json()["correction_event_id"]).status_code == 409   # a reversal cannot be reversed
        skip = add_event(engine, s, "STAGE", kind="SKIP", details={"reason": "later"})
        assert error_code(reverse(apps, "stadium", skip)) == "NOT_REVERSIBLE"
        assert reverse(apps, "stadium", "00000000-0000-0000-0000-000000000000").status_code == 404
        assert reverse(apps, "stadium", "not-a-uuid").status_code == 404
        assert reverse(apps, "stadium", ids["THOBE_ALLOCATION"], "still works after the bad ids").status_code == 200  # no poisoned state

    def test_two_admins_reversing_the_same_record_at_once_write_exactly_one_reversal(self, apps, engine, world):
        s, ids = journey_upto(engine, "QUEUE")
        clients = [new_client(apps["stadium"]) for _ in range(6)]
        for c in clients:
            assert api_login(c, "eng-admin").status_code == 200
        results = _run_threads(lambda i: reverse(apps, "stadium", ids["SEATING"], f"race {i}", client=clients[i]).status_code, 6)
        assert sorted(results) == [200, 409, 409, 409, 409, 409], results
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE kind = 'REVERSAL' AND corrects_event_id = :e", e=ids["SEATING"]) == 1

    def test_a_correction_that_fails_half_way_leaves_nothing_behind(self, apps, engine, monkeypatch):
        s, ids = journey_upto(engine, "QUEUE")
        before = counts(engine)

        def boom(*a, **k):
            raise RuntimeError("simulated crash between the event and its audit row")
        monkeypatch.setattr(corrections, "insert_audit", boom)
        r = reverse(apps, "stadium", ids["SEATING"])
        assert r.status_code == 503 and "One moment" in r.json()["detail"]["message"] and "RuntimeError" not in r.text
        assert counts(engine) == before                                    # no reversal, no outbox row, no audit row
        assert scalar(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id) in ("SEATED / NOT QUEUED", "REPORTED / MONEY PENDING")  # still derived as before
        monkeypatch.undo()
        assert reverse(apps, "stadium", ids["SEATING"]).status_code == 200  # and it works once the fault is gone

    def test_after_a_reversal_the_student_can_be_done_again_and_duplicates_are_still_blocked(self, apps, engine, world):
        s = make_student(engine)
        reporting = operator(apps, world, "REGISTRATION")
        first = confirm(reporting, "REGISTRATION", token=s.token).json()
        assert first["result"] == "CONFIRMED"
        original = scalar(engine, "SELECT event_id FROM activity_events WHERE student_id = :s AND kind = 'COMPLETE'", s=s.id)
        assert confirm(reporting, "REGISTRATION", token=s.token).json()["result"] == "DUPLICATE"
        assert reverse(apps, "college", original, "wrong student").status_code == 200
        assert scan(reporting, "REGISTRATION", s.token).json()["result"] == "READY"       # reopened, by a NEW row
        assert confirm(reporting, "REGISTRATION", token=s.token).json()["result"] == "CONFIRMED"
        assert confirm(reporting, "REGISTRATION", token=s.token).json()["result"] == "DUPLICATE"
        cycles = rows(engine, "SELECT kind, completion_cycle FROM activity_events WHERE student_id = :s ORDER BY server_time", s=s.id)
        assert [(r["kind"], r["completion_cycle"]) for r in cycles] == [("COMPLETE", 1), ("REVERSAL", 1), ("COMPLETE", 2)]
        assert scalar(engine, "SELECT kind FROM activity_events WHERE event_id = :e", e=original) == "COMPLETE"  # cycle 1 still there

    def test_admin_can_reverse_any_activity_across_all_stations(self, apps, engine):
        s, ids = journey_upto(engine, "LUNCH")
        before = counts(engine)
        r = reverse(apps, "stadium", ids["THOBE_RETURN"])
        assert r.status_code == 200
        assert counts(engine)["activity_events"] == before["activity_events"] + 1

    def test_the_stage_queue_follows_a_reversal_but_the_led_is_never_touched(self, apps, engine):
        # Stage reversed: back to QUEUED at the old position.
        s, ids = journey_upto(engine, "THOBE_RETURN")
        with engine.begin() as c:
            c.execute(text("INSERT INTO queue (student_id, status) VALUES (:s, 'DONE')"), {"s": s.id})
        position = scalar(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=s.id)
        led_before = table_fingerprint(engine, "stage_state", "id")
        assert reverse(apps, "stadium", ids["STAGE"]).status_code == 200
        assert rows(engine, "SELECT status, queue_position FROM queue WHERE student_id = :s", s=s.id) == [{"status": "QUEUED", "queue_position": position}]
        assert table_fingerprint(engine, "stage_state", "id") == led_before               # golden rule 9
        # Queue reversed: the student must not be shown on stage any more.
        assert reverse(apps, "stadium", ids["QUEUE"]).status_code == 200
        assert scalar(engine, "SELECT count(*) FROM queue WHERE student_id = :s", s=s.id) == 0
        assert table_fingerprint(engine, "stage_state", "id") == led_before

    def test_a_student_on_stage_right_now_cannot_have_their_queue_reversed(self, apps, engine):
        s, ids = journey_upto(engine, "STAGE")
        with engine.begin() as c:
            c.execute(text("INSERT INTO queue (student_id, status) VALUES (:s, 'DISPLAYED')"), {"s": s.id})
        before = counts(engine)
        r = reverse(apps, "stadium", ids["QUEUE"])
        assert r.status_code == 409 and error_code(r) == "ON_STAGE_NOW" and counts(engine) == before
        assert scalar(engine, "SELECT status FROM queue WHERE student_id = :s", s=s.id) == "DISPLAYED"

    def test_it_reports_later_activities_that_are_still_recorded(self, apps, engine):
        s, ids = journey_upto(engine, "LUNCH")
        r = reverse(apps, "stadium", ids["THOBE_ALLOCATION"], "issued to the wrong person").json()
        assert r["later_activities_still_recorded"] == ["QUEUE", "SEATING", "STAGE", "THOBE_RETURN"]


class TestHistoryCannotBeChangedByAnyPath:
    def test_no_code_in_the_application_updates_or_deletes_activity_events(self):
        """The static half of the guarantee: there is nothing in backend/ that could."""
        pattern = re.compile(r"(UPDATE\s+activity_events|DELETE\s+FROM\s+activity_events|TRUNCATE[^;\n]*activity_events)", re.I)
        offenders = [(str(p.relative_to(REPO)), m.group(0)) for p in (REPO / "backend").rglob("*.py")
                     for m in pattern.finditer(p.read_text(encoding="utf-8"))]
        assert offenders == []

    def test_the_only_activity_events_writes_in_the_corrections_module_are_one_insert_and_selects(self):
        source = (REPO / "backend" / "admin" / "corrections.py").read_text(encoding="utf-8")
        statements = re.findall(r'text\(\s*"(\w+)[^"]*activity_events', source)
        assert set(statements) <= {"INSERT", "SELECT"} and statements.count("INSERT") == 1, statements

    def test_the_database_itself_refuses_to_change_a_corrected_record(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        assert reverse(apps, "stadium", ids["SEATING"]).status_code == 200
        before = physical_row(engine, ids["SEATING"])
        for sql in ("UPDATE activity_events SET kind = 'SKIP' WHERE event_id = :e",
                    "UPDATE activity_events SET details = '{}' WHERE event_id = :e",
                    "UPDATE activity_events SET flags = flags WHERE event_id = :e",       # even a no-op update
                    "DELETE FROM activity_events WHERE event_id = :e"):
            with engine.connect() as c:
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql), {"e": ids["SEATING"]})
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION, sql
        with engine.connect() as c:
            with pytest.raises(DBAPIError) as exc:
                c.execute(text("TRUNCATE activity_events"))
            assert exc.value.orig.pgcode == RESTRICT_VIOLATION
        assert physical_row(engine, ids["SEATING"]) == before

    def test_the_audit_log_is_append_only_too(self, engine):
        for sql in ("UPDATE audit_log SET reason = 'edited'", "DELETE FROM audit_log"):
            with engine.connect() as c:
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql))
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION


# =========================================================================== RETURN WAIVED / LOST
def lunch_scan(apps, world, student):
    return scan(operator(apps, world, "LUNCH"), "LUNCH", student.token).json()


class TestReturnWaived:
    def make(self, engine):
        s = make_student(engine)
        from tests.test_station_engine import seed_events
        seed_events(engine, s, ACTIVITIES[:ACTIVITIES.index("THOBE_RETURN")] + ["MONEY_RETURNED"])  # through Stage; money back, robe never returned
        return s

    def test_the_waiver_unlocks_lunch_is_flagged_and_fully_audited(self, apps, engine, world):
        s = self.make(engine)
        blocked = lunch_scan(apps, world, s)
        assert blocked["result"] == "REJECTED" and blocked["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"
        allocation = scalar(engine, "SELECT event_id FROM activity_events WHERE student_id = :s AND activity = 'THOBE_ALLOCATION'", s=s.id)
        before = counts(engine)

        r = waive(apps, "hall", s, "student reports the robe was lost")
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "WAIVER" and r.json()["thobe_allocation_on_record"] is True

        event = rows(engine, "SELECT * FROM activity_events WHERE event_id = :e", e=r.json()["correction_event_id"])[0]
        assert event["kind"] == "WAIVER" and event["activity"] == "THOBE_RETURN"
        assert "CORRECTED" in event["flags"] and event["operator_id"] == world.admin_id
        assert event["details"]["reason"] == "student reports the robe was lost" and event["completion_cycle"] == 1
        assert scalar(engine, "SELECT count(*) FROM activity_events") == before["activity_events"] + 1   # one new row, nothing else
        audit = rows(engine, "SELECT * FROM audit_log WHERE event_id = :e", e=event["event_id"])[0]
        assert audit["action"] == "RETURN_WAIVED" and audit["corrected_by"] == world.admin_id and "CORRECTED" in audit["flags"]
        assert audit["reason"] == "student reports the robe was lost" and audit["corrects_event_id"] == allocation  # links to the robe written off
        ex = rows(engine, "SELECT * FROM exceptions WHERE event_id = :e", e=event["event_id"])[0]
        assert ex["type"] == "RETURN_WAIVED" and ex["status"] == "OPEN" and ex["student_id"] == s.id

        ready = lunch_scan(apps, world, s)                               # Lunch is unlocked
        assert ready["result"] == "READY"
        assert confirm(operator(apps, world, "LUNCH"), "LUNCH", token=s.token).json()["result"] == "CONFIRMED"
        assert scalar(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id) == "EXITED"

    def test_it_shows_in_the_waived_list_and_leaves_the_outstanding_list(self, apps, engine):
        s = self.make(engine)
        outstanding = lambda: {r["prn"] for r in admin(apps, "hall").get("/admin/api/reports/outstanding-robes").json()["rows"]}  # noqa: E731
        waived = lambda: {r["prn"]: r for r in admin(apps, "hall").get("/admin/api/reports/waived-robes").json()["rows"]}  # noqa: E731
        assert s.prn in outstanding() and s.prn not in waived()
        assert waive(apps, "hall", s, "lost in transit").status_code == 200
        assert s.prn not in outstanding() and waived()[s.prn]["reason"] == "lost in transit"
        assert waived()[s.prn]["waived_by"] == "eng-admin"
        assert any(c["prn"] == s.prn and c["kind"] == "Return waived / lost"
                   for c in admin(apps, "hall").get("/admin/api/reports/corrections").json()["rows"])
        open_types = admin(apps, "hall").get("/admin/api/exceptions", params={"status": "OPEN", "type": "RETURN_WAIVED"}).json()["exceptions"]
        assert any(x["prn"] == s.prn and x["reason"] == "lost in transit" for x in open_types)

    def test_reason_is_mandatory_server_side(self, apps, engine):
        s = self.make(engine)
        before = counts(engine)
        for bad in (None, "", "  \t "):
            r = waive(apps, "hall", s, bad)
            assert r.status_code == 400 and error_code(r) == "REASON_REQUIRED", repr(bad)
        assert counts(engine) == before

    @pytest.mark.parametrize("role", OPERATOR_ROLES)
    def test_an_operator_gets_403(self, apps, engine, world, role):
        s = self.make(engine)
        client = operator(apps, world, role)
        before = counts(engine)
        other = new_client(apps["hall"])
        other.headers["Authorization"] = f"Bearer {client.cookies.get('session')}"
        r = waive(apps, "hall", s, client=other)
        assert r.status_code == 403 and error_code(r) == "FORBIDDEN"
        assert counts(engine) == before

    def test_it_cannot_be_used_when_the_thobe_is_already_returned_or_waived(self, apps, engine):
        s = self.make(engine)
        add_event(engine, s, "THOBE_RETURN")
        r = waive(apps, "hall", s)
        assert r.status_code == 409 and error_code(r) == "ALREADY_RETURNED"
        t = self.make(engine)
        assert waive(apps, "hall", t).status_code == 200
        assert error_code(waive(apps, "hall", t)) == "ALREADY_RETURNED"
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s AND kind = 'WAIVER'", s=t.id) == 1

    def test_two_admins_waiving_at_once_write_one_waiver(self, apps, engine):
        s = self.make(engine)
        clients = [new_client(apps["hall"]) for _ in range(5)]
        for c in clients:
            assert api_login(c, "eng-admin").status_code == 200
        results = _run_threads(lambda i: waive(apps, "hall", s, f"race {i}", client=clients[i]).status_code, 5)
        assert sorted(results) == [200, 409, 409, 409, 409], results
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s AND kind = 'WAIVER'", s=s.id) == 1

    def test_admin_can_waive_from_any_session(self, apps, engine):
        s = self.make(engine)
        before = counts(engine)
        r = waive(apps, "college", s)
        assert r.status_code == 200
        assert counts(engine)["activity_events"] == before["activity_events"] + 1

    def test_unknown_students_and_bad_ids(self, apps):
        assert waive(apps, "hall", "00000000-0000-0000-0000-000000000000").status_code == 404
        assert waive(apps, "hall", "nonsense").status_code == 404

    def test_a_waiver_can_itself_be_reversed_and_lunch_locks_again(self, apps, engine, world):
        s = self.make(engine)
        w = waive(apps, "hall", s).json()["correction_event_id"]
        assert lunch_scan(apps, world, s)["result"] == "READY"
        assert reverse(apps, "hall", w, "waived the wrong student").status_code == 200
        assert lunch_scan(apps, world, s)["result"] == "REJECTED"
        again = waive(apps, "hall", s, "second, correct waiver")           # the cycle counter lets a new waiver in
        assert again.status_code == 200
        assert scalar(engine, "SELECT completion_cycle FROM activity_events WHERE event_id = :e", e=again.json()["correction_event_id"]) == 2
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE event_id = :e AND kind = 'WAIVER'", e=w) == 1  # the first still exists

    def test_a_waiver_without_an_allocation_on_record_is_allowed_but_says_so(self, apps, engine):
        s = add_student(engine, school="School of Law")
        r = waive(apps, "hall", s, "student insists a robe was issued at the Stadium")
        assert r.status_code == 200 and r.json()["thobe_allocation_on_record"] is False
        assert scalar(engine, "SELECT corrects_event_id FROM audit_log WHERE event_id = :e", e=r.json()["correction_event_id"]) is None


# =========================================================================== EXCEPTIONS
class TestExceptions:
    def new_exception(self, engine, type_="CONFLICT"):
        with engine.begin() as c:
            return c.execute(text("INSERT INTO exceptions (type) VALUES (:t) RETURNING id"), {"t": type_}).scalar_one()

    def test_resolve_needs_a_note_records_who_and_when_and_is_audited(self, apps, engine, world):
        xid = self.new_exception(engine)
        client = admin(apps, "hall")
        before = scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'EXCEPTION_RESOLVED'")
        for bad in ("", "   "):
            r = client.post(f"/admin/api/exceptions/{xid}/resolve", json={"note": bad})
            assert r.status_code == 400 and error_code(r) == "NOTE_REQUIRED"
        assert scalar(engine, "SELECT status FROM exceptions WHERE id = :i", i=xid) == "OPEN"
        ok = client.post(f"/admin/api/exceptions/{xid}/resolve", json={"note": "checked the register, duplicate is genuine"})
        assert ok.status_code == 200
        row = rows(engine, "SELECT * FROM exceptions WHERE id = :i", i=xid)[0]
        assert row["status"] == "RESOLVED" and row["resolved_by"] == world.admin_id and row["resolved_at"] is not None
        assert row["resolution_note"] == "checked the register, duplicate is genuine"
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'EXCEPTION_RESOLVED'") == before + 1

    def test_a_resolved_item_is_final_and_unknown_items_are_404(self, apps, engine):
        xid = self.new_exception(engine)
        client = admin(apps, "hall")
        assert client.post(f"/admin/api/exceptions/{xid}/resolve", json={"note": "done"}).status_code == 200
        again = client.post(f"/admin/api/exceptions/{xid}/resolve", json={"note": "overwrite attempt"})
        assert again.status_code == 409 and error_code(again) == "ALREADY_RESOLVED"
        assert scalar(engine, "SELECT resolution_note FROM exceptions WHERE id = :i", i=xid) == "done"   # never overwritten
        assert client.post("/admin/api/exceptions/999999/resolve", json={"note": "x"}).status_code == 404
        assert client.post("/admin/api/exceptions/abc/resolve", json={"note": "x"}).status_code == 404

    def test_the_database_lets_an_exception_be_resolved_and_nothing_else(self, engine):
        """Migration 0007: exceptions can only move OPEN -> RESOLVED; never edited, re-resolved or deleted."""
        open_id, done_id = self.new_exception(engine, "CONFLICT"), self.new_exception(engine, "SEQ_GAP")
        with engine.begin() as c:
            c.execute(text("UPDATE exceptions SET status='RESOLVED', resolved_at=now(), resolution_note='ok' WHERE id=:i"), {"i": done_id})
        attempts = [
            ("UPDATE exceptions SET type = 'OTHER' WHERE id = :i", open_id),
            ("UPDATE exceptions SET details = '{\"x\": 1}' WHERE id = :i", open_id),
            ("UPDATE exceptions SET student_id = NULL, created_at = now() WHERE id = :i", open_id),
            ("UPDATE exceptions SET resolution_note = 'changed' WHERE id = :i", done_id),
            ("UPDATE exceptions SET status = 'OPEN', resolved_at = NULL WHERE id = :i", done_id),
            ("DELETE FROM exceptions WHERE id = :i", open_id),
            ("DELETE FROM exceptions WHERE id = :i", done_id),
        ]
        for sql, i in attempts:
            with engine.connect() as c:
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql), {"i": i})
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION, sql
        with engine.connect() as c:
            with pytest.raises(DBAPIError) as exc:
                c.execute(text("TRUNCATE exceptions"))
            assert exc.value.orig.pgcode == RESTRICT_VIOLATION

    def test_the_list_filters_by_status_and_type(self, apps, engine):
        a, b = self.new_exception(engine, "INACTIVE_TOKEN_USED"), self.new_exception(engine, "INACTIVE_TOKEN_USED")
        client = admin(apps, "hall")
        client.post(f"/admin/api/exceptions/{b}/resolve", json={"note": "fine"})
        open_ids = {x["id"] for x in client.get("/admin/api/exceptions", params={"status": "OPEN", "type": "INACTIVE_TOKEN_USED"}).json()["exceptions"]}
        done_ids = {x["id"] for x in client.get("/admin/api/exceptions", params={"status": "RESOLVED", "type": "INACTIVE_TOKEN_USED"}).json()["exceptions"]}
        assert a in open_ids and b not in open_ids and b in done_ids and a not in done_ids

    def test_the_pages_work_and_the_form_resolves(self, apps, engine):
        xid = self.new_exception(engine, "SEQ_GAP")
        client = admin(apps, "hall")
        page = client.get("/admin/exceptions")
        assert page.status_code == 200 and f"/admin/exceptions/{xid}/resolve" in page.text
        r = client.post(f"/admin/exceptions/{xid}/resolve", data={"note": "gap explained"}, follow_redirects=False)
        assert r.status_code == 303 and "resolved" in r.headers["location"].lower()
        assert scalar(engine, "SELECT status FROM exceptions WHERE id = :i", i=xid) == "RESOLVED"
        bad = client.post(f"/admin/exceptions/{self.new_exception(engine)}/resolve", data={"note": " "}, follow_redirects=False)
        assert bad.status_code == 303 and "error=" in bad.headers["location"]


# =========================================================================== SEARCH, JOURNEY, AUDIT
class TestSearchAndJourney:
    def test_search_by_prn_name_and_sequence_number_and_wildcards_are_literal(self, apps, engine):
        s = add_student(engine, school="School of Arts", name="Zoë Xylophone-Qwerty")
        client = admin(apps, "hall")
        find = lambda q: [x["student_id"] for x in client.get("/admin/api/students", params={"q": q}).json()["students"]]  # noqa: E731
        assert find(s.prn) == find(s.prn.lower()) == [str(s.id)]
        assert find("xylophone-qwe") == [str(s.id)]
        assert find(str(s.seq)) == [str(s.id)]
        # A blank (or whitespace-only) query is not "no results": it is the first page of every
        # student, the same thing an admin sees on first opening the screen before typing anything.
        # That is more useful for browsing the roster than an empty list would be.
        total_students = scalar(engine, "SELECT count(*) FROM students")
        assert len(find("")) == len(find("   ")) == min(total_students, 25)
        assert find("%") == [] and find("_") == []            # a typed wildcard matches nothing special
        assert find("no-one-has-this-name") == []

    def test_the_journey_timeline_shows_every_event_with_its_state(self, apps, engine, world):
        s, ids = journey_upto(engine, "THOBE_RETURN")
        reverse(apps, "stadium", ids["STAGE"], "pressed by accident")
        j = admin(apps, "stadium").get(f"/admin/api/students/{s.id}").json()
        assert j["student"]["prn"] == s.prn and j["student"]["journey_status"] == "Degree not received"
        by_activity = [(e["activity"], e["kind"], e["state"]) for e in j["events"]]
        assert by_activity == [("REGISTRATION", "COMPLETE", "ACTIVE"), ("THOBE_ALLOCATION", "COMPLETE", "ACTIVE"),
                               ("SEATING", "COMPLETE", "ACTIVE"),
                               ("QUEUE", "COMPLETE", "ACTIVE"), ("STAGE", "COMPLETE", "REVERSED"), ("STAGE", "REVERSAL", "CORRECTION")]
        stage, correction = j["events"][4], j["events"][5]
        assert stage["reversed_by_event_id"] == correction["event_id"] and correction["corrects_event_id"] == stage["event_id"]
        assert correction["reason"] == "pressed by accident" and correction["operator"] == "eng-admin"
        assert j["events"][0]["can_reverse"] and not stage["can_reverse"]
        assert j["can_waive_return"] is True

    def test_the_journey_page_and_unknown_students(self, apps, engine):
        s, ids = journey_upto(engine, "SEATING")
        page = admin(apps, "stadium").get(f"/admin/students/{s.id}")
        assert page.status_code == 200 and s.prn in page.text and "Reverse" in page.text
        assert admin(apps, "stadium").get("/admin/students/00000000-0000-0000-0000-000000000000").status_code == 404
        assert admin(apps, "stadium").get("/admin/api/students/not-a-uuid").status_code == 404
        assert admin(apps, "stadium").get(f"/admin/students?q={s.prn}").status_code == 200

    def test_the_journey_page_forms_make_the_same_corrections(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        client = admin(apps, "stadium")
        bad = client.post(f"/admin/students/{s.id}/reverse", data={"event_id": str(ids["SEATING"]), "reason": " "}, follow_redirects=False)
        assert bad.status_code == 303 and "error=" in bad.headers["location"]
        good = client.post(f"/admin/students/{s.id}/reverse", data={"event_id": str(ids["SEATING"]), "reason": "form test"}, follow_redirects=False)
        assert good.status_code == 303 and "msg=" in good.headers["location"]
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE kind = 'REVERSAL' AND student_id = :s", s=s.id) == 1


class TestAudit:
    def test_the_audit_viewer_filters_and_pages(self, apps, engine):
        s, ids = journey_upto(engine, "QUEUE")
        reverse(apps, "stadium", ids["SEATING"], "audit view test")
        client = admin(apps, "stadium")
        body = client.get("/admin/api/audit", params={"student": s.prn}).json()
        assert body["total"] == 1 and body["rows"][0]["action"] == "ADMIN_REVERSAL"
        row = body["rows"][0]
        assert row["corrected_by"] == "eng-admin" and row["reason"] == "audit view test" and row["corrects_event_id"] == str(ids["SEATING"])
        assert row["prn"] == s.prn and row["activity"] == "SEATING" and "CORRECTED" in row["flags"]
        assert client.get("/admin/api/audit", params={"action": "ADMIN_REVERSAL", "limit": 1}).json()["rows"].__len__() == 1
        assert client.get("/admin/api/audit", params={"action": "NO_SUCH_ACTION"}).json()["total"] == 0
        assert client.get("/admin/api/audit", params={"since": "not a date"}).status_code == 400
        assert client.get("/admin/api/audit", params={"since": "2000-01-01", "until": "2100-01-01"}).json()["total"] >= 1
        assert client.get("/admin/audit", params={"student": s.prn}).status_code == 200

    def test_the_audit_export_round_trips_and_the_export_itself_is_logged(self, apps, engine, world):
        s = add_student(engine, school="School of Law", name="Łukasz Đorđević 山田")
        add_event(engine, s, "REGISTRATION")
        with engine.begin() as c:
            from backend.audit import write_audit
            write_audit(c, "TEST_ENTRY", operator_id=world.admin_id, student_id=s.id, reason="Ünïcode reason – ok", details={"note": "日本語"})
        client = admin(apps, "hall")
        before = scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'EXPORT'")
        r = client.get("/admin/api/audit/export", params={"format": "csv", "student": s.prn})
        assert r.status_code == 200
        header, parsed = parse_csv(r.content)
        assert len(parsed) == 1 and parsed[0]["Student"] == "Łukasz Đorđević 山田" and parsed[0]["Reason"] == "Ünïcode reason – ok"
        assert parsed[0]["Operator"] == "eng-admin" and "日本語" in parsed[0]["Details"] and "Audit id" in header
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'EXPORT'") == before + 1
        last = rows(engine, "SELECT operator_id, details FROM audit_log WHERE action = 'EXPORT' ORDER BY id DESC LIMIT 1")[0]
        assert last["operator_id"] == world.admin_id and last["details"]["report"] == "audit" and last["details"]["filters"] == {"student": s.prn}
        assert client.get("/admin/api/audit/export", params={"format": "xlsx"}).status_code == 200


# =========================================================================== WHO MAY REACH WHAT
def admin_console_routes(app=None):
    """Every route the Admin console router registers, read from the router itself, so a route added to it later is
    covered automatically and cannot be forgotten by this test. (The requests below go through the real app.)"""
    from backend.admin.routes import router
    return [(method, route.path) for route in router.routes for method in sorted(route.methods - {"HEAD", "OPTIONS"})]


def concrete(path):
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


class TestAdminOnly:
    def test_the_route_table_is_really_being_read(self, apps):
        routes = admin_console_routes(apps["hall"])
        paths = {p for _, p in routes}
        assert len(routes) >= 20
        assert {"/admin/api/corrections/reverse", "/admin/api/corrections/waive-return", "/admin/api/audit/export",
                "/admin/api/reports/{key}/export", "/admin/api/dashboard", "/admin/students/{student_id}/reverse",
                "/admin/students/{student_id}/waive-return", "/admin/exceptions/{exception_id}/resolve"} <= paths

    @pytest.mark.parametrize("role", OPERATOR_ROLES)
    def test_every_admin_console_route_refuses_every_operator_with_403(self, apps, world, role):
        client = operator(apps, world, role)
        token = client.cookies.get("session")
        for venue in ("hall", "central"):
            other = new_client(apps[venue])
            other.headers["Authorization"] = f"Bearer {token}"
            for method, path in admin_console_routes(apps[venue]):
                kwargs = {"json": {}} if method == "POST" else {}
                r = other.request(method, concrete(path), follow_redirects=False, **kwargs)
                assert r.status_code == 403, (role, venue, method, path, r.status_code)

    def test_every_admin_console_route_refuses_a_signed_out_visitor(self, apps):
        for method, path in admin_console_routes(apps["hall"]):
            anon = new_client(apps["hall"])
            kwargs = {"json": {}} if method == "POST" else {}
            r = anon.request(method, concrete(path), follow_redirects=False, headers={"accept": "application/json"}, **kwargs)
            assert r.status_code == 401, (method, path, r.status_code)

    def test_an_operators_refused_export_writes_no_audit_row_and_no_data_leaves(self, apps, engine, world):
        client = operator(apps, world, "LUNCH")
        before = scalar(engine, "SELECT count(*) FROM audit_log")
        other = new_client(apps["hall"])
        other.headers["Authorization"] = f"Bearer {client.cookies.get('session')}"
        for path in ("/admin/api/reports/not-attended/export?format=csv", "/admin/api/audit/export?format=csv",
                     "/admin/api/reports/student-history/export?format=xlsx", "/admin/api/reports/corrections", "/admin/api/audit"):
            r = other.get(path)
            assert r.status_code == 403 and b"PRN" not in r.content and b"Audit id" not in r.content, path
        assert scalar(engine, "SELECT count(*) FROM audit_log") == before

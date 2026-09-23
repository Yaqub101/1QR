"""The Registry desk (approved role/flow redesign, Phase R1).

ONE merged operator role, REGISTRY, replaces the Reporting, Robe Allocation and Robe Return operators.
The student meets the Registry desk twice:

  * ON ENTRY  - ONE scan, ONE confirm. It records BOTH Reporting (REGISTRATION) and Robe Allocation
                (THOBE_ALLOCATION) in ONE transaction: both are saved, or neither is.
  * AFTER STAGE - the same scan at the same desk records Robe Return (THOBE_RETURN), a plain confirmation.

The desk works out which of the two it is from the student's own record, so the operator never picks
(golden rule 2: the operator does not choose the activity). Everything underneath is the unchanged station
engine: the two events are ordinary per-activity events, each still guarded by the database's per-activity
unique constraint, each with its audit row and scan_log row.

Seating is no longer a prerequisite of anything: the Queue now needs the robe, not a seat.

Every message and expectation below is written out by hand, not imported from the implementation.
"""
from unittest import mock

import pytest

from backend.engine import service
from tests.test_schema import _run_threads
from tests.test_station_engine import (  # noqa: F401  (engine/world/apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    assert_plain,
    clock,
    engine,
    events_of,
    log_of,
    make_student,
    operator,
    q,
    scan,
    seed_events,
    world,
)

REGISTRY = "REGISTRY"
ENTRY_LABEL = "CONFIRM REPORTING + ROBE"
ROBE_ONLY_LABEL = "CONFIRM ROBE GIVEN"
RETURN_LABEL = "CONFIRM ROBE RETURN"
ALREADY_ENTERED = "ALREADY REPORTED AND ROBE GIVEN — {time}"
ALREADY_RETURNED = "ALREADY RETURNED — {time}"
UNKNOWN_QR = "QR NOT RECOGNISED — use PRN search or contact Admin"
INACTIVE = "STUDENT NOT ACTIVE — CONTACT ADMIN"
READY = "Check the photo, then confirm."
DONE = "Done."
TEMPORARY = "One moment, please try again."
UP_TO_STAGE = ["REGISTRATION", "THOBE_ALLOCATION", "QUEUE", "STAGE"]  # no seating: it is optional now


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()  # clients cached by other modules belong to a database that no longer exists
    yield
    _CLIENTS.clear()


def desk(apps, world):
    """The Registry operator's own signed-in client. (eng-registration holds the REGISTRY role.)"""
    return operator(apps, world, "REGISTRATION")


def registry_scan(client, token):
    return client.post("/scan", json={"activity": REGISTRY, "token": token})


def registry_search(client, prn):
    return client.post("/search", json={"activity": REGISTRY, "prn": prn})


def registry_confirm(client, *, token=None, student_id=None, step=None):
    body = {"activity": REGISTRY}
    if token is not None:
        body["token"] = token
    if student_id is not None:
        body["student_id"] = str(student_id)
    if step is not None:
        body["step"] = step
    return client.post("/confirm", json=body)


def completions(engine, student):
    """{activity: number of live completions} for one student."""
    rows = q(engine, "SELECT activity, count(*) AS n FROM activity_events WHERE student_id = :s "
                     "AND kind IN ('COMPLETE','WAIVER') GROUP BY activity", s=student.id)
    return {r["activity"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Entry: one scan, one confirm, two events in one transaction
# --------------------------------------------------------------------------- #
class TestEntry:
    def test_a_new_student_scans_ready_for_the_combined_entry_confirm(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "READY" and reply["message"] == READY
        assert reply["step"] == "ENTRY" and reply["confirm_label"] == ENTRY_LABEL
        assert reply["student"]["name"] == s.name
        assert completions(engine, s) == {}  # a scan never records an activity

    def test_one_confirm_records_reporting_and_robe_allocation_together(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_scan(client, s.token)
        reply = registry_confirm(client, token=s.token, step="ENTRY").json()
        assert reply["result"] == "CONFIRMED" and reply["message"] == DONE
        assert completions(engine, s) == {"REGISTRATION": 1, "THOBE_ALLOCATION": 1}
        reg, robe = events_of(engine, s, "REGISTRATION")[0], events_of(engine, s, "THOBE_ALLOCATION")[0]
        assert reg["operator_id"] == robe["operator_id"] == world.op_ids["REGISTRATION"]
        # ONE transaction: PostgreSQL's now() is the transaction start time, so both events carry the same instant.
        assert reg["server_time"] == robe["server_time"]
        audits = q(engine, "SELECT activity, event_id FROM audit_log WHERE student_id = :s AND action = 'ACTIVITY_CONFIRMED'",
                   s=s.id)
        assert {a["activity"] for a in audits} == {"REGISTRATION", "THOBE_ALLOCATION"}
        assert {r["result"] for r in log_of(engine, s, "REGISTRATION")} >= {"SUCCESS"}
        assert {r["result"] for r in log_of(engine, s, "THOBE_ALLOCATION")} == {"SUCCESS"}

    def test_scanning_again_after_entry_is_a_plain_amber_duplicate_and_writes_nothing(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY")
        when = clock(events_of(engine, s, "REGISTRATION")[0]["server_time"])
        for reply in (registry_scan(client, s.token).json(),
                      registry_confirm(client, token=s.token, step="ENTRY").json()):
            assert reply["result"] == "DUPLICATE" and reply["colour"] == "amber"
            assert reply["message"] == ALREADY_ENTERED.format(time=when)
            assert_plain(reply["message"])
        assert completions(engine, s) == {"REGISTRATION": 1, "THOBE_ALLOCATION": 1}

    def test_if_the_robe_half_fails_the_reporting_half_is_not_saved_either(self, apps, world, engine):
        s = make_student(engine)
        real_insert = service.insert_event

        def fail_on_robe(conn, ctx, student, **kw):
            if ctx.activity == "THOBE_ALLOCATION":
                raise RuntimeError("simulated crash between the two writes")
            return real_insert(conn, ctx, student, **kw)

        with mock.patch.object(service, "insert_event", side_effect=fail_on_robe):
            response = registry_confirm(desk(apps, world), token=s.token, step="ENTRY")
        assert response.status_code == 503
        assert response.json()["detail"]["message"] == TEMPORARY
        assert completions(engine, s) == {}  # nothing half-written survives
        assert q(engine, "SELECT count(*) AS n FROM audit_log WHERE student_id = :s AND action = 'ACTIVITY_CONFIRMED'",
                 s=s.id)[0]["n"] == 0

    def test_two_desks_confirming_the_same_student_at_once_record_exactly_one_pair(self, apps, world, engine):
        from tests.test_auth import api_login, new_client

        s = make_student(engine)
        clients = []
        for _ in range(6):
            c = new_client(apps)
            assert api_login(c, "eng-registration").status_code == 200
            clients.append(c)
        replies = _run_threads(lambda i: registry_confirm(clients[i], token=s.token, step="ENTRY").json(), 6)
        results = sorted(r["result"] for r in replies)
        assert results.count("CONFIRMED") == 1 and results.count("DUPLICATE") == 5, results
        assert completions(engine, s) == {"REGISTRATION": 1, "THOBE_ALLOCATION": 1}

    def test_a_student_reported_earlier_without_a_robe_gets_only_the_robe(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION"])
        client = desk(apps, world)
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "READY" and reply["step"] == "ROBE" and reply["confirm_label"] == ROBE_ONLY_LABEL
        assert registry_confirm(client, token=s.token, step="ROBE").json()["result"] == "CONFIRMED"
        assert completions(engine, s) == {"REGISTRATION": 1, "THOBE_ALLOCATION": 1}

    def test_reporting_after_the_cutoff_is_still_flagged_late(self, apps, world, engine):
        s = make_student(engine)
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE settings SET late_cutoff = now() - interval '1 minute' WHERE id = 1")
        try:
            registry_confirm(desk(apps, world), token=s.token, step="ENTRY")
        finally:
            with engine.begin() as c:
                c.exec_driver_sql("UPDATE settings SET late_cutoff = NULL WHERE id = 1")
        assert "LATE" in events_of(engine, s, "REGISTRATION")[0]["flags"]
        assert "LATE" not in events_of(engine, s, "THOBE_ALLOCATION")[0]["flags"]


# --------------------------------------------------------------------------- #
# The same desk, later: Robe Return
# --------------------------------------------------------------------------- #
class TestReturn:
    def test_after_stage_the_same_scan_offers_robe_return(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "READY" and reply["step"] == "RETURN" and reply["confirm_label"] == RETURN_LABEL
        done = registry_confirm(client, token=s.token, step="RETURN").json()
        assert done["result"] == "CONFIRMED" and done["message"] == DONE
        assert completions(engine, s)["THOBE_RETURN"] == 1
        assert events_of(engine, s, "THOBE_RETURN")[0]["details"] == {}  # robes are unnumbered: nothing to record

    def test_a_second_return_scan_is_a_duplicate(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="RETURN")
        when = clock(events_of(engine, s, "THOBE_RETURN")[0]["server_time"])
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"] == ALREADY_RETURNED.format(time=when)
        assert completions(engine, s)["THOBE_RETURN"] == 1

    def test_an_admin_waiver_counts_as_returned(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "THOBE_RETURN"],
                    kind_overrides={"THOBE_RETURN": "WAIVER"})
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"].startswith("ALREADY RETURNED — ")

    def test_before_stage_the_desk_says_already_entered_and_offers_no_return(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "QUEUE"])
        client = desk(apps, world)
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"].startswith("ALREADY REPORTED AND ROBE GIVEN — ")
        assert registry_confirm(client, token=s.token, step="RETURN").json()["result"] != "CONFIRMED"
        assert "THOBE_RETURN" not in completions(engine, s)

    def test_a_confirm_for_a_step_the_student_is_no_longer_at_writes_nothing(self, apps, world, engine):
        """The operator was shown the ENTRY card; by the time they press confirm the student is somewhere
        else. The desk must never record an action the operator was not shown."""
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        before = completions(engine, s)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY").json()
        assert reply["result"] != "CONFIRMED"
        assert_plain(reply["message"])
        assert completions(engine, s) == before


# --------------------------------------------------------------------------- #
# Refusals, manual PRN fallback, access
# --------------------------------------------------------------------------- #
class TestRefusalsAndFallback:
    def test_an_unknown_qr_is_refused_plainly(self, apps, world, engine):
        reply = registry_scan(desk(apps, world), "NOT-A-REAL-TOKEN").json()
        assert reply["result"] == "INVALID" and reply["message"] == UNKNOWN_QR

    def test_an_inactive_student_is_refused_and_nothing_is_written(self, apps, world, engine):
        s = make_student(engine, active=False)
        client = desk(apps, world)
        assert registry_scan(client, s.token).json()["message"] == INACTIVE
        assert registry_confirm(client, token=s.token, step="ENTRY").json()["result"] == "REJECTED"
        assert completions(engine, s) == {}

    def test_manual_prn_search_then_confirm_flags_both_events_manual(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        found = registry_search(client, s.prn).json()
        assert found["result"] == "READY" and found["manual"] is True and found["step"] == "ENTRY"
        reply = registry_confirm(client, student_id=found["student"]["student_id"], step="ENTRY").json()
        assert reply["result"] == "CONFIRMED"
        for activity in ("REGISTRATION", "THOBE_ALLOCATION"):
            assert "MANUAL" in events_of(engine, s, activity)[0]["flags"]

    @pytest.mark.parametrize("activity", ["SEATING", "QUEUE", "STAGE", "LUNCH"])
    def test_other_operators_cannot_use_the_registry_desk(self, apps, world, engine, activity):
        s = make_student(engine)
        client = operator(apps, world, activity)
        assert client.get("/station/registry").status_code == 403
        for response in (registry_scan(client, s.token), registry_confirm(client, token=s.token, step="ENTRY")):
            assert response.status_code == 403
            assert response.json()["detail"]["message"] == "That screen is not part of your role."
        assert completions(engine, s) == {}

    def test_a_registry_operator_cannot_confirm_lunch_or_queue(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE + ["THOBE_RETURN"])
        client = desk(apps, world)
        for activity in ("LUNCH", "QUEUE"):
            response = client.post("/confirm", json={"activity": activity, "token": s.token})
            assert response.status_code == 403
            assert response.json()["detail"]["message"] == "That screen is not part of your role."
        assert "LUNCH" not in completions(engine, s)

    def test_the_admin_can_use_the_registry_desk(self, apps, world, engine):
        s = make_student(engine)
        assert admin(apps).get("/station/registry").status_code == 200
        assert registry_confirm(admin(apps), token=s.token, step="ENTRY").json()["result"] == "CONFIRMED"


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
class TestRegistryScreen:
    def test_the_desk_screen_has_the_scan_box_camera_and_prn_fallback(self, apps, world):
        page = desk(apps, world).get("/station/registry")
        assert page.status_code == 200
        assert 'data-activity="REGISTRY"' in page.text
        assert 'id="scan"' in page.text and "autofocus" in page.text
        assert 'id="camera-details"' in page.text and "/static/camera_scan.js" in page.text
        assert 'id="search-prn"' in page.text
        assert "<h1>Registry</h1>" in page.text


# --------------------------------------------------------------------------- #
# Seating no longer blocks anything; Lunch still needs the return
# --------------------------------------------------------------------------- #
class TestJourneyWithoutSeating:
    def test_the_queue_accepts_a_student_with_a_robe_and_no_seat(self, apps, world, engine):
        s = make_student(engine)
        registry_confirm(desk(apps, world), token=s.token, step="ENTRY")
        reply = scan(operator(apps, world, "QUEUE"), "QUEUE", s.token).json()
        assert reply["result"] == "READY", reply["message"]

    def test_the_queue_refuses_a_student_with_no_robe(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION"])
        reply = scan(operator(apps, world, "QUEUE"), "QUEUE", s.token).json()
        assert reply["result"] == "REJECTED" and reply["message"] == "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"

    def test_seating_is_still_available_and_still_optional(self, apps, world, engine):
        s = make_student(engine)
        registry_confirm(desk(apps, world), token=s.token, step="ENTRY")
        assert scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()["result"] == "READY"

    def test_lunch_still_needs_the_robe_back(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        lunch = operator(apps, world, "LUNCH")
        assert scan(lunch, "LUNCH", s.token).json()["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"
        registry_confirm(desk(apps, world), token=s.token, step="RETURN")
        assert scan(lunch, "LUNCH", s.token).json()["result"] == "READY"

    def test_the_whole_redesigned_journey_uses_three_qr_scan_points(self, apps, world, engine):
        """Registry (entry) -> Queue -> [Stage, no scan] -> Registry (return) -> Lunch."""
        s = make_student(engine)
        client = desk(apps, world)
        assert registry_confirm(client, token=s.token, step="ENTRY").json()["result"] == "CONFIRMED"
        queue = operator(apps, world, "QUEUE")
        assert queue.post("/confirm", json={"activity": "QUEUE", "token": s.token}).json()["result"] == "CONFIRMED"
        seed_events(engine, s, ["STAGE"])  # Stage is driven by the Stage operator, not a scan (Phase R2)
        assert registry_confirm(client, token=s.token, step="RETURN").json()["result"] == "CONFIRMED"
        lunch = operator(apps, world, "LUNCH")
        assert lunch.post("/confirm", json={"activity": "LUNCH", "token": s.token}).json()["result"] == "CONFIRMED"
        assert completions(engine, s) == {a: 1 for a in ("REGISTRATION", "THOBE_ALLOCATION", "QUEUE", "STAGE",
                                                          "THOBE_RETURN", "LUNCH")}
        status = q(engine, "SELECT status FROM student_status WHERE student_id = :s", s=s.id)[0]["status"]
        assert status == "EXITED"


# --------------------------------------------------------------------------- #
# The migration: existing accounts with the three merged roles become Registry accounts
# --------------------------------------------------------------------------- #
class TestRoleMigration:
    def test_existing_reporting_robe_and_return_accounts_become_registry_and_are_audited(self, bare_database):
        from sqlalchemy import create_engine, text

        from tests.conftest import run_alembic

        assert run_alembic("upgrade", "0013_robe_status_labels", database_url=bare_database).returncode == 0
        eng = create_engine(bare_database)
        try:
            with eng.begin() as c:
                for username, role in (("old-reg", "REGISTRATION"), ("old-robe", "THOBE_ALLOCATION"),
                                       ("old-ret", "THOBE_RETURN"), ("old-lunch", "LUNCH"), ("old-admin", "ADMIN")):
                    c.execute(text("INSERT INTO users (username, password_hash, role) VALUES (:u, 'h', :r)"),
                              {"u": username, "r": role})
            result = run_alembic("upgrade", "head", database_url=bare_database)
            assert result.returncode == 0, result.stdout + result.stderr
            with eng.connect() as c:
                roles = dict(c.execute(text("SELECT username, role FROM users")).all())
                audits = c.execute(text("SELECT details->>'username' AS u, details->>'from_role' AS f, "
                                        "details->>'to_role' AS t FROM audit_log WHERE action = 'ROLE_MERGED'")).all()
            assert roles == {"old-reg": "REGISTRY", "old-robe": "REGISTRY", "old-ret": "REGISTRY",
                             "old-lunch": "LUNCH", "old-admin": "ADMIN"}
            assert sorted(audits) == [("old-reg", "REGISTRATION", "REGISTRY"), ("old-ret", "THOBE_RETURN", "REGISTRY"),
                                      ("old-robe", "THOBE_ALLOCATION", "REGISTRY")]
        finally:
            eng.dispose()

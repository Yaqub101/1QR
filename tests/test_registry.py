"""The Registry desk with tick boxes (role/flow redesign R1, reworked in Phase R4).

ONE merged operator role, REGISTRY, meets the student at the desk. Each scan shows where the student is (their
journey status) and a pair of tick boxes; the desk decides WHICH pair from the student's own record, so the
operator never chooses the activity (golden rule 2):

  * ENTRY  (until both robe and money are recorded): "Robe allotted" and "Money received".
      - The first confirm always records Reporting, with or without any box ticked, so a student can be
        registered even before the robe or the money is sorted out.
      - Scanning again shows the same two boxes, the recorded ones already done, until both are recorded.
  * RETURN (after the degree, i.e. the Stage event): "Robe returned" and "Money returned", ticked separately.
  * Otherwise a plain amber sentence: received and waiting for the ceremony, or everything returned.

Every tick is its own activity (THOBE_ALLOCATION, MONEY_RECEIVED, THOBE_RETURN, MONEY_RETURNED) with its own
per-activity unique constraint, audit row and scan_log row; everything one confirm records is ONE transaction.
The Queue needs the robe AND the money; Lunch needs both returns (an Admin may waive either return).

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
ROBE, ROBE_BACK = "THOBE_ALLOCATION", "THOBE_RETURN"
RECEIVED_WAIT = "ROBE ALLOTTED — COME BACK AFTER THE CEREMONY"
ALL_RETURNED = "ROBE ALREADY RETURNED — {time}"
TICK_ONE = "Tick at least one box, then confirm."
CHANGED = "The record has just changed; check the boxes and confirm again."
UNKNOWN_QR = "QR NOT RECOGNISED — use PRN search or contact Admin"
INACTIVE = "STUDENT NOT ACTIVE — CONTACT ADMIN"
READY = "Check the photo, tick what you hand over, then confirm."
READY_RETURN = "Check the photo, tick what you take back, then confirm."
DONE = "Done."
TEMPORARY = "One moment, please try again."
ENTRY_DONE = ["REGISTRATION", ROBE]
UP_TO_STAGE = ENTRY_DONE + ["QUEUE", "STAGE"]


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


def registry_confirm(client, *, token=None, student_id=None, step=None, marks=()):
    body = {"activity": REGISTRY, "marks": list(marks)}
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


def markers(reply):
    return [(m["key"], m["label"], m["done"]) for m in reply["markers"]]


def status(engine, student):
    return q(engine, "SELECT status FROM student_status WHERE student_id = :s", s=student.id)[0]["status"]


# --------------------------------------------------------------------------- #
# Entry: register, with or without the boxes
# --------------------------------------------------------------------------- #
class TestEntry:
    def test_a_new_student_shows_the_entry_box_unticked_and_where_they_are(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "READY" and reply["message"] == READY and reply["step"] == "ENTRY"
        assert markers(reply) == [(ROBE, "Robe allotted", False)]
        assert reply["state"] == "REGISTERED / NOT REPORTED"
        assert reply["confirm_label"] == "CONFIRM"
        assert completions(engine, s) == {}  # a scan never records an activity

    def test_confirming_with_no_box_ticked_still_registers_the_student(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[]).json()
        assert reply["result"] == "CONFIRMED" and reply["message"] == DONE
        assert completions(engine, s) == {"REGISTRATION": 1}
        assert reply["state"] == "REPORTED / ROBE PENDING"

    def test_robe_box_on_the_first_confirm_records_two_events_in_one_transaction(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[ROBE]).json()
        assert reply["result"] == "CONFIRMED"
        assert [e["activity"] for e in reply["events"]] == ["REGISTRATION", ROBE]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1}
        times = {events_of(engine, s, a)[0]["server_time"] for a in ENTRY_DONE}
        assert len(times) == 1  # ONE transaction: PostgreSQL's now() is the transaction start time
        assert reply["state"] == status(engine, s) == "ROBE RECEIVED / NOT QUEUED"
        audits = q(engine, "SELECT activity FROM audit_log WHERE student_id = :s AND action = 'ACTIVITY_CONFIRMED'", s=s.id)
        assert sorted(a["activity"] for a in audits) == sorted(ENTRY_DONE)
        for activity in ENTRY_DONE:
            assert "SUCCESS" in {r["result"] for r in log_of(engine, s, activity)}

    def test_scanning_again_shows_the_same_box_with_what_is_already_done(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["step"] is None
        assert reply["message"] == RECEIVED_WAIT
        assert markers(reply) == [(ROBE, "Robe allotted", True)]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1}

    def test_robe_then_report_works(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        assert status(engine, s) == "ROBE RECEIVED / NOT QUEUED"

    def test_a_reported_student_must_have_a_box_ticked(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[])
        reply = registry_confirm(client, token=s.token, step="ENTRY", marks=[]).json()
        assert reply["result"] == "REJECTED" and reply["message"] == TICK_ONE
        assert_plain(reply["message"])
        assert completions(engine, s) == {"REGISTRATION": 1}

    def test_once_robe_is_recorded_the_desk_says_come_back_after_the_ceremony(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        for reply in (registry_scan(client, s.token).json(),
                      registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE]).json()):
            assert reply["result"] == "DUPLICATE" and reply["colour"] == "amber"
            assert reply["message"] == RECEIVED_WAIT and reply["step"] is None
            assert markers(reply) == [(ROBE, "Robe allotted", True)]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1}

    def test_a_box_somebody_else_just_ticked_is_not_recorded_twice(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[])  # register
        # Robe already done by another desk — ENTRY is now "DUPLICATE" so no stale-screen scenario
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1}

    @pytest.mark.parametrize("bad", [[ROBE_BACK], ["LUNCH"], ["QUEUE"], ["NOT_A_THING"]])
    def test_a_box_that_does_not_belong_to_this_step_records_nothing(self, apps, world, engine, bad):
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=bad).json()
        assert reply["result"] != "CONFIRMED"
        assert completions(engine, s) == {}

    def test_if_one_write_fails_nothing_of_that_confirm_is_saved(self, apps, world, engine):
        s = make_student(engine)
        real_insert = service.insert_event

        def fail_on_robe(conn, ctx, student, **kw):
            if ctx.activity == ROBE:
                raise RuntimeError("simulated crash between the writes")
            return real_insert(conn, ctx, student, **kw)

        with mock.patch.object(service, "insert_event", side_effect=fail_on_robe):
            response = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[ROBE])
        assert response.status_code == 503 and response.json()["detail"]["message"] == TEMPORARY
        assert completions(engine, s) == {}
        assert q(engine, "SELECT count(*) AS n FROM audit_log WHERE student_id = :s AND action = 'ACTIVITY_CONFIRMED'",
                 s=s.id)[0]["n"] == 0

    def test_two_desks_confirming_the_same_student_at_once_record_each_event_once(self, apps, world, engine):
        from tests.test_auth import api_login, new_client

        s = make_student(engine)
        clients = []
        for _ in range(6):
            c = new_client(apps)
            assert api_login(c, "eng-registration").status_code == 200
            clients.append(c)
        replies = _run_threads(
            lambda i: registry_confirm(clients[i], token=s.token, step="ENTRY", marks=[ROBE]).json(), 6)
        assert [r["result"] for r in replies].count("CONFIRMED") == 1, replies
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1}

    def test_reporting_after_the_cutoff_is_still_flagged_late(self, apps, world, engine):
        s = make_student(engine)
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE settings SET late_cutoff = now() - interval '1 minute' WHERE id = 1")
        try:
            registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[ROBE])
        finally:
            with engine.begin() as c:
                c.exec_driver_sql("UPDATE settings SET late_cutoff = NULL WHERE id = 1")
        assert "LATE" in events_of(engine, s, "REGISTRATION")[0]["flags"]
        assert "LATE" not in events_of(engine, s, ROBE)[0]["flags"]


# --------------------------------------------------------------------------- #
# After the degree: the return boxes
# --------------------------------------------------------------------------- #
class TestReturn:
    def test_after_the_degree_the_same_scan_shows_the_return_box(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "READY" and reply["step"] == "RETURN" and reply["message"] == READY_RETURN
        assert markers(reply) == [(ROBE_BACK, "Robe returned", False)]
        assert reply["state"] == "ROBE NOT RETURNED"

    def test_robe_return_and_then_lunch_eligible(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        first = registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()
        assert first["result"] == "CONFIRMED" and first["state"] == "LUNCH ELIGIBLE"
        assert events_of(engine, s, ROBE_BACK)[0]["details"] == {}  # robes are unnumbered: nothing to record

    def test_robe_returned_and_then_a_plain_duplicate(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        assert registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()["result"] == "CONFIRMED"
        when = clock(events_of(engine, s, ROBE_BACK)[0]["server_time"])
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"] == ALL_RETURNED.format(time=when)
        assert markers(reply) == [(ROBE_BACK, "Robe returned", True)]

    def test_before_the_degree_there_are_no_return_boxes(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ENTRY_DONE + ["QUEUE"])
        client = desk(apps, world)
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"] == RECEIVED_WAIT
        assert registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()["result"] != "CONFIRMED"
        assert ROBE_BACK not in completions(engine, s)

    def test_admin_waivers_count_as_returned(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE + [ROBE_BACK],
                    kind_overrides={ROBE_BACK: "WAIVER"})
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"].startswith("ROBE ALREADY RETURNED — ")


# --------------------------------------------------------------------------- #
# Refusals, manual PRN fallback, access, the screen
# --------------------------------------------------------------------------- #
class TestRefusalsAndFallback:
    def test_an_unknown_qr_is_refused_plainly(self, apps, world, engine):
        reply = registry_scan(desk(apps, world), "NOT-A-REAL-TOKEN").json()
        assert reply["result"] == "INVALID" and reply["message"] == UNKNOWN_QR

    def test_an_inactive_student_is_refused_and_nothing_is_written(self, apps, world, engine):
        s = make_student(engine, active=False)
        client = desk(apps, world)
        assert registry_scan(client, s.token).json()["message"] == INACTIVE
        assert registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE]).json()["result"] == "REJECTED"
        assert completions(engine, s) == {}

    def test_manual_prn_search_then_confirm_flags_every_event_manual(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        found = registry_search(client, s.prn).json()
        assert found["result"] == "READY" and found["manual"] is True and found["step"] == "ENTRY"
        reply = registry_confirm(client, student_id=found["student"]["student_id"], step="ENTRY", marks=[ROBE]).json()
        assert reply["result"] == "CONFIRMED"
        for activity in ENTRY_DONE:
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
        seed_events(engine, s, UP_TO_STAGE + [ROBE_BACK])
        client = desk(apps, world)
        for activity in ("LUNCH", "QUEUE"):
            response = client.post("/confirm", json={"activity": activity, "token": s.token})
            assert response.status_code == 403
        assert "LUNCH" not in completions(engine, s)

    def test_the_admin_can_use_the_registry_desk(self, apps, world, engine):
        s = make_student(engine)
        assert admin(apps).get("/station/registry").status_code == 200
        assert registry_confirm(admin(apps), token=s.token, step="ENTRY", marks=[ROBE]).json()["result"] == "CONFIRMED"


class TestRegistryScreen:
    def test_the_desk_screen_has_the_scan_box_the_boxes_area_camera_and_prn_fallback(self, apps, world):
        page = desk(apps, world).get("/station/registry")
        assert page.status_code == 200
        assert 'data-activity="REGISTRY"' in page.text and "<h1>Registry</h1>" in page.text
        assert "autofocus" not in page.text
        assert 'id="markers"' in page.text and 'id="card-state"' in page.text
        assert 'id="camera-details"' in page.text and "camera_scan.js" in page.text
        assert 'id="search-prn"' in page.text


# --------------------------------------------------------------------------- #
# What the other scan points now need
# --------------------------------------------------------------------------- #
class TestJourney:
    def test_the_queue_needs_the_robe(self, apps, world, engine):
        queue = operator(apps, world, "QUEUE")
        no_robe = make_student(engine)
        seed_events(engine, no_robe, ["REGISTRATION"])
        with_robe = make_student(engine)
        seed_events(engine, with_robe, ENTRY_DONE)
        assert scan(queue, "QUEUE", no_robe.token).json()["message"] == "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"
        assert scan(queue, "QUEUE", with_robe.token).json()["result"] == "READY"  # no seat needed

    def test_lunch_needs_the_robe_back(self, apps, world, engine):
        lunch = operator(apps, world, "LUNCH")
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        assert scan(lunch, "LUNCH", s.token).json()["message"] == "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"
        registry_confirm(desk(apps, world), token=s.token, step="RETURN", marks=[ROBE_BACK])
        assert scan(lunch, "LUNCH", s.token).json()["result"] == "READY"

    def test_the_admin_can_waive_the_robe_return_so_a_student_can_still_have_lunch(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        adm = admin(apps)
        robe = adm.post("/admin/api/corrections/waive-return", json={"student_id": str(s.id), "reason": "Robe lost"})
        assert robe.status_code == 200, robe.text
        assert status(engine, s) == "LUNCH ELIGIBLE"
        again = adm.post("/admin/api/corrections/waive-return", json={"student_id": str(s.id), "reason": "twice"})
        assert again.status_code == 409
        assert operator(apps, world, "REGISTRATION").post(
            "/admin/api/corrections/waive-return", json={"student_id": str(s.id), "reason": "x"}).status_code == 403

    def test_the_whole_journey_through_the_three_scan_points(self, apps, world, engine):
        """Registry (entry) -> Queue -> [Stage] -> Registry (return) -> Lunch."""
        s = make_student(engine)
        client = desk(apps, world)
        assert registry_confirm(client, token=s.token, step="ENTRY", marks=[]).json()["result"] == "CONFIRMED"
        assert registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE]).json()["result"] == "CONFIRMED"
        queue = operator(apps, world, "QUEUE")
        assert queue.post("/confirm", json={"activity": "QUEUE", "token": s.token}).json()["result"] == "CONFIRMED"
        seed_events(engine, s, ["STAGE"])  # Stage is the Stage operator's NEXT, not a scan
        assert registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()["result"] == "CONFIRMED"
        lunch = operator(apps, world, "LUNCH")
        assert lunch.post("/confirm", json={"activity": "LUNCH", "token": s.token}).json()["result"] == "CONFIRMED"
        assert completions(engine, s) == {a: 1 for a in UP_TO_STAGE + [ROBE_BACK, "LUNCH"]}
        assert status(engine, s) == "EXITED"


# --------------------------------------------------------------------------- #
# The migrations
# --------------------------------------------------------------------------- #
class TestMigrations:
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

    def test_migration_0020_installs_the_seven_activity_student_status_view(self, bare_database):
        from sqlalchemy import create_engine, text

        from tests.conftest import run_alembic

        assert run_alembic("upgrade", "head", database_url=bare_database).returncode == 0
        eng = create_engine(bare_database)
        try:
            with eng.begin() as c:
                sid = c.execute(text("INSERT INTO students (prn, name, programme, school) VALUES "
                                     "('MIG2', 'Mig Two', 'P', 'S') RETURNING id")).scalar_one()
                # Seed the minimum activities to reach the return step
                for activity in ('REGISTRATION', 'THOBE_ALLOCATION', 'QUEUE', 'STAGE'):
                    c.execute(text(
                        "INSERT INTO activity_events (student_id, activity, kind, operator_id, details) "
                        "VALUES (:s, :a, 'COMPLETE', gen_random_uuid(), '{}')"
                    ), {"s": sid, "a": activity})
                # THOBE_RETURN waiver is still allowed in the 7-activity flow
                c.execute(text(
                    "INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details) "
                    "VALUES (:s, 'THOBE_RETURN', 'WAIVER', gen_random_uuid(), '{CORRECTED}', "
                    "'{\"reason\": \"lost\"}')"
                ), {"s": sid})
            with eng.connect() as c:
                status = c.execute(text("SELECT status FROM student_status WHERE student_id = :s"),
                                   {"s": sid}).scalar()
            assert status == "LUNCH ELIGIBLE"
        finally:
            eng.dispose()


# --------------------------------------------------------------------------- #
# Regression: All Checkmark Permutations and Full End-to-End Flow (Phase R4)
# --------------------------------------------------------------------------- #
class TestPermutationsAndFullFlow:
    @pytest.mark.parametrize("marks,expected_events", [
        ([], ["REGISTRATION"]),
        ([ROBE], ["REGISTRATION", ROBE]),
    ])
    def test_registry_op_confirm_entry_permutations(self, apps, world, engine, marks, expected_events):
        """Registry OP confirm for every entry checkmark permutation: [], [ROBE]."""
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=marks).json()
        assert reply["result"] == "CONFIRMED"
        assert [e["activity"] for e in reply["events"]] == expected_events
        assert completions(engine, s) == {a: 1 for a in expected_events}

    @pytest.mark.parametrize("marks,expected_events", [
        ([ROBE_BACK], [ROBE_BACK]),
    ])
    def test_registry_op_confirm_return_permutations(self, apps, world, engine, marks, expected_events):
        """Registry OP confirm for the return checkmark: [ROBE_BACK]."""
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        reply = registry_confirm(desk(apps, world), token=s.token, step="RETURN", marks=marks).json()
        assert reply["result"] == "CONFIRMED"
        assert [e["activity"] for e in reply["events"]] == expected_events
        for a in expected_events:
            assert completions(engine, s).get(a) == 1

    def test_full_flow_registry_queue_stage_return_lunch(self, apps, world, engine):
        """Full flow: Registry -> Queue -> Stage -> Return -> Lunch, on a database built with alembic upgrade head."""
        from sqlalchemy import text
        s = make_student(engine)
        # Ensure display snapshot exists for Stage display
        with engine.begin() as c:
            c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school, award) "
                           "VALUES (:s, :n, 'B.Tech Computer Science', 'School of Engineering', 'Degree') "
                           "ON CONFLICT DO NOTHING"), {"s": s.id, "n": s.name})

        reg_client = desk(apps, world)
        queue_client = operator(apps, world, "QUEUE")
        stage_client = operator(apps, world, "STAGE")
        lunch_client = operator(apps, world, "LUNCH")

        # 1. Registry Desk: Reporting + Robe in one confirm
        scan1 = registry_scan(reg_client, s.token).json()
        assert scan1["result"] == "READY"
        conf1 = registry_confirm(reg_client, token=s.token, step="ENTRY", marks=[ROBE]).json()
        assert conf1["result"] == "CONFIRMED"
        assert status(engine, s) == "ROBE RECEIVED / NOT QUEUED"

        # 2. Queue Station: Scan & Confirm (needs robe; seating is optional)
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.stage_controller', 'on', true)"))
            c.execute(text("UPDATE stage_state SET current_student_id = NULL, display_student_id = NULL, "
                           "previous_student_id = NULL, controller_session_id = NULL WHERE id = 1"))
            c.execute(text("UPDATE queue SET status = 'DONE' WHERE status IN ('QUEUED','DISPLAYED','HELD','SKIPPED')"))

        scan2 = queue_client.post("/scan", json={"activity": "QUEUE", "token": s.token}).json()
        assert scan2["result"] == "READY"
        conf2 = queue_client.post("/confirm", json={"activity": "QUEUE", "token": s.token}).json()
        assert conf2["result"] == "CONFIRMED"
        assert status(engine, s) == "DEGREE NOT RECEIVED"

        # 3. Stage: Claim controller, NEXT shows student, NEXT records degree
        stage_client.post("/stage/control", json={"station_id": "STG-01"})
        nxt1 = stage_client.post("/stage/next", json={"expect_current": None})
        assert nxt1.status_code == 200
        nxt2 = stage_client.post("/stage/next", json={"expect_current": str(s.id)})
        assert nxt2.status_code == 200
        assert status(engine, s) == "ROBE NOT RETURNED"

        # 4. Registry Return: Robe Returned
        scan3 = registry_scan(reg_client, s.token).json()
        assert scan3["result"] == "READY" and scan3["step"] == "RETURN"
        conf3 = registry_confirm(reg_client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()
        assert conf3["result"] == "CONFIRMED"
        assert status(engine, s) == "LUNCH ELIGIBLE"

        # 5. Lunch Station: Scan & Confirm (needs robe back)
        scan4 = lunch_client.post("/scan", json={"activity": "LUNCH", "token": s.token}).json()
        assert scan4["result"] == "READY"
        conf4 = lunch_client.post("/confirm", json={"activity": "LUNCH", "token": s.token}).json()
        assert conf4["result"] == "CONFIRMED"
        assert status(engine, s) == "EXITED"


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
ROBE, MONEY, ROBE_BACK, MONEY_BACK = "THOBE_ALLOCATION", "MONEY_RECEIVED", "THOBE_RETURN", "MONEY_RETURNED"
RECEIVED_WAIT = "ROBE AND MONEY RECEIVED — COME BACK AFTER THE CEREMONY"
ALL_RETURNED = "ROBE AND MONEY ALREADY RETURNED — {time}"
TICK_ONE = "Tick at least one box, then confirm."
CHANGED = "The record has just changed; check the boxes and confirm again."
UNKNOWN_QR = "QR NOT RECOGNISED — use PRN search or contact Admin"
INACTIVE = "STUDENT NOT ACTIVE — CONTACT ADMIN"
READY = "Check the photo, tick what you hand over, then confirm."
READY_RETURN = "Check the photo, tick what you take back, then confirm."
DONE = "Done."
TEMPORARY = "One moment, please try again."
ENTRY_DONE = ["REGISTRATION", ROBE, MONEY]
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
    def test_a_new_student_shows_the_two_entry_boxes_unticked_and_where_they_are(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "READY" and reply["message"] == READY and reply["step"] == "ENTRY"
        assert markers(reply) == [(ROBE, "Robe allotted", False), (MONEY, "Money received", False)]
        assert reply["state"] == "REGISTERED / NOT REPORTED"
        assert reply["confirm_label"] == "CONFIRM"
        assert completions(engine, s) == {}  # a scan never records an activity

    def test_confirming_with_no_box_ticked_still_registers_the_student(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[]).json()
        assert reply["result"] == "CONFIRMED" and reply["message"] == DONE
        assert completions(engine, s) == {"REGISTRATION": 1}
        assert reply["state"] == "REPORTED / ROBE AND MONEY PENDING"

    def test_both_boxes_on_the_first_confirm_record_three_events_in_one_transaction(self, apps, world, engine):
        s = make_student(engine)
        reply = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[ROBE, MONEY]).json()
        assert reply["result"] == "CONFIRMED"
        assert [e["activity"] for e in reply["events"]] == ["REGISTRATION", ROBE, MONEY]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1, MONEY: 1}
        times = {events_of(engine, s, a)[0]["server_time"] for a in ENTRY_DONE}
        assert len(times) == 1  # ONE transaction: PostgreSQL's now() is the transaction start time
        assert reply["state"] == status(engine, s) == "ROBE AND MONEY RECEIVED / NOT QUEUED"
        audits = q(engine, "SELECT activity FROM audit_log WHERE student_id = :s AND action = 'ACTIVITY_CONFIRMED'", s=s.id)
        assert sorted(a["activity"] for a in audits) == sorted(ENTRY_DONE)
        for activity in ENTRY_DONE:
            assert "SUCCESS" in {r["result"] for r in log_of(engine, s, activity)}

    def test_scanning_again_shows_the_same_two_boxes_with_what_is_already_done(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "READY" and reply["step"] == "ENTRY"
        assert markers(reply) == [(ROBE, "Robe allotted", True), (MONEY, "Money received", False)]
        assert reply["markers"][0]["time"] is not None and reply["markers"][1]["time"] is None
        assert reply["state"] == "REPORTED / MONEY PENDING"
        done = registry_confirm(client, token=s.token, step="ENTRY", marks=[MONEY]).json()
        assert done["result"] == "CONFIRMED" and [e["activity"] for e in done["events"]] == [MONEY]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1, MONEY: 1}  # Reporting is not written twice

    def test_money_first_then_robe_works_the_other_way_round(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[MONEY])
        assert status(engine, s) == "REPORTED / ROBE PENDING"
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])
        assert status(engine, s) == "ROBE AND MONEY RECEIVED / NOT QUEUED"

    def test_a_reported_student_must_have_a_box_ticked(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[])
        reply = registry_confirm(client, token=s.token, step="ENTRY", marks=[]).json()
        assert reply["result"] == "REJECTED" and reply["message"] == TICK_ONE
        assert_plain(reply["message"])
        assert completions(engine, s) == {"REGISTRATION": 1}

    def test_once_both_are_recorded_the_desk_says_come_back_after_the_ceremony(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE, MONEY])
        for reply in (registry_scan(client, s.token).json(),
                      registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE]).json()):
            assert reply["result"] == "DUPLICATE" and reply["colour"] == "amber"
            assert reply["message"] == RECEIVED_WAIT and reply["step"] is None
            assert markers(reply) == [(ROBE, "Robe allotted", True), (MONEY, "Money received", True)]
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1, MONEY: 1}

    def test_a_box_somebody_else_just_ticked_is_not_recorded_twice(self, apps, world, engine):
        s = make_student(engine)
        client = desk(apps, world)
        registry_confirm(client, token=s.token, step="ENTRY", marks=[])
        registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE])  # another desk, a moment ago
        reply = registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE, MONEY]).json()  # stale screen
        assert reply["result"] == "READY" and reply["message"] == CHANGED
        assert markers(reply) == [(ROBE, "Robe allotted", True), (MONEY, "Money received", False)]
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

        def fail_on_money(conn, ctx, student, **kw):
            if ctx.activity == MONEY:
                raise RuntimeError("simulated crash between the writes")
            return real_insert(conn, ctx, student, **kw)

        with mock.patch.object(service, "insert_event", side_effect=fail_on_money):
            response = registry_confirm(desk(apps, world), token=s.token, step="ENTRY", marks=[ROBE, MONEY])
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
            lambda i: registry_confirm(clients[i], token=s.token, step="ENTRY", marks=[ROBE, MONEY]).json(), 6)
        assert [r["result"] for r in replies].count("CONFIRMED") == 1, replies
        assert completions(engine, s) == {"REGISTRATION": 1, ROBE: 1, MONEY: 1}

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
    def test_after_the_degree_the_same_scan_shows_the_two_return_boxes(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "READY" and reply["step"] == "RETURN" and reply["message"] == READY_RETURN
        assert markers(reply) == [(ROBE_BACK, "Robe returned", False), (MONEY_BACK, "Money returned", False)]
        assert reply["state"] == "ROBE AND MONEY NOT RETURNED"

    def test_robe_and_money_can_come_back_separately(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        first = registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK]).json()
        assert first["result"] == "CONFIRMED" and first["state"] == "MONEY NOT RETURNED"
        again = registry_scan(client, s.token).json()
        assert again["step"] == "RETURN"
        assert markers(again) == [(ROBE_BACK, "Robe returned", True), (MONEY_BACK, "Money returned", False)]
        registry_confirm(client, token=s.token, step="RETURN", marks=[MONEY_BACK])
        assert status(engine, s) == "LUNCH ELIGIBLE"
        assert events_of(engine, s, ROBE_BACK)[0]["details"] == {}  # robes are unnumbered: nothing to record

    def test_both_back_in_one_confirm_and_then_a_plain_duplicate(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        client = desk(apps, world)
        assert registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK, MONEY_BACK]).json()["result"] == "CONFIRMED"
        when = clock(events_of(engine, s, MONEY_BACK)[0]["server_time"])
        reply = registry_scan(client, s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"] == ALL_RETURNED.format(time=when)
        assert markers(reply) == [(ROBE_BACK, "Robe returned", True), (MONEY_BACK, "Money returned", True)]

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
        seed_events(engine, s, UP_TO_STAGE + [ROBE_BACK, MONEY_BACK],
                    kind_overrides={ROBE_BACK: "WAIVER", MONEY_BACK: "WAIVER"})
        reply = registry_scan(desk(apps, world), s.token).json()
        assert reply["result"] == "DUPLICATE" and reply["message"].startswith("ROBE AND MONEY ALREADY RETURNED — ")


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
        reply = registry_confirm(client, student_id=found["student"]["student_id"], step="ENTRY", marks=[ROBE, MONEY]).json()
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
        seed_events(engine, s, UP_TO_STAGE + [ROBE_BACK, MONEY_BACK])
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
        assert 'id="scan"' in page.text and "autofocus" in page.text
        assert 'id="markers"' in page.text and 'id="card-state"' in page.text
        assert 'id="camera-details"' in page.text and "camera_scan.js" in page.text
        assert 'id="search-prn"' in page.text


# --------------------------------------------------------------------------- #
# What the other scan points now need
# --------------------------------------------------------------------------- #
class TestJourney:
    def test_the_queue_needs_the_robe_and_the_money(self, apps, world, engine):
        queue = operator(apps, world, "QUEUE")
        robe_only, money_only, both = make_student(engine), make_student(engine), make_student(engine)
        seed_events(engine, robe_only, ["REGISTRATION", ROBE])
        seed_events(engine, money_only, ["REGISTRATION", MONEY])
        seed_events(engine, both, ENTRY_DONE)
        assert scan(queue, "QUEUE", robe_only.token).json()["message"] == "QUEUE NOT AVAILABLE — MONEY NOT RECEIVED"
        assert scan(queue, "QUEUE", money_only.token).json()["message"] == "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"
        assert scan(queue, "QUEUE", both.token).json()["result"] == "READY"  # no seat needed

    def test_lunch_needs_both_returns(self, apps, world, engine):
        lunch = operator(apps, world, "LUNCH")
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE + [ROBE_BACK])
        assert scan(lunch, "LUNCH", s.token).json()["message"] == "LUNCH NOT AVAILABLE — MONEY RETURN PENDING"
        registry_confirm(desk(apps, world), token=s.token, step="RETURN", marks=[MONEY_BACK])
        assert scan(lunch, "LUNCH", s.token).json()["result"] == "READY"

    def test_the_admin_can_record_money_kept_so_a_student_whose_robe_was_lost_can_still_have_lunch(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, UP_TO_STAGE)
        adm = admin(apps)
        robe = adm.post("/admin/api/corrections/waive-return", json={"student_id": str(s.id), "reason": "Robe lost"})
        money = adm.post("/admin/api/corrections/waive-money", json={"student_id": str(s.id), "reason": "Kept for the lost robe"})
        assert robe.status_code == 200 and money.status_code == 200, (robe.text, money.text)
        assert money.json()["message"] == "Money kept. The student can now go to Lunch."  # the robe was already settled
        assert [e["kind"] for e in events_of(engine, s, MONEY_BACK)] == ["WAIVER"]
        assert "CORRECTED" in events_of(engine, s, MONEY_BACK)[0]["flags"]
        assert status(engine, s) == "LUNCH ELIGIBLE"
        again = adm.post("/admin/api/corrections/waive-money", json={"student_id": str(s.id), "reason": "twice"})
        assert again.status_code == 409
        assert operator(apps, world, "REGISTRATION").post(
            "/admin/api/corrections/waive-money", json={"student_id": str(s.id), "reason": "x"}).status_code == 403

    def test_the_whole_journey_through_the_three_scan_points(self, apps, world, engine):
        """Registry (entry) -> Queue -> [Stage] -> Registry (returns) -> Lunch."""
        s = make_student(engine)
        client = desk(apps, world)
        assert registry_confirm(client, token=s.token, step="ENTRY", marks=[]).json()["result"] == "CONFIRMED"
        assert registry_confirm(client, token=s.token, step="ENTRY", marks=[ROBE, MONEY]).json()["result"] == "CONFIRMED"
        queue = operator(apps, world, "QUEUE")
        assert queue.post("/confirm", json={"activity": "QUEUE", "token": s.token}).json()["result"] == "CONFIRMED"
        seed_events(engine, s, ["STAGE"])  # Stage is the Stage operator's NEXT, not a scan
        assert registry_confirm(client, token=s.token, step="RETURN", marks=[ROBE_BACK, MONEY_BACK]).json()["result"] == "CONFIRMED"
        lunch = operator(apps, world, "LUNCH")
        assert lunch.post("/confirm", json={"activity": "LUNCH", "token": s.token}).json()["result"] == "CONFIRMED"
        assert completions(engine, s) == {a: 1 for a in UP_TO_STAGE + [ROBE_BACK, MONEY_BACK, "LUNCH"]}
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

    def test_the_money_activities_are_accepted_only_after_their_migration_and_waivers_extend_to_money(self, bare_database):
        from sqlalchemy import create_engine, text

        from tests.conftest import run_alembic

        assert run_alembic("upgrade", "0016_optional_seating_labels", database_url=bare_database).returncode == 0
        eng = create_engine(bare_database)
        insert = ("INSERT INTO activity_events (student_id, activity, kind, operator_id, details) "
                  "VALUES (:s, :a, :k, gen_random_uuid(), CAST(:d AS jsonb))")
        try:
            with eng.begin() as c:
                sid = c.execute(text("INSERT INTO students (prn, name, programme, school) VALUES "
                                     "('MIG1', 'Mig One', 'P', 'S') RETURNING id")).scalar_one()
            with eng.connect() as c:
                with pytest.raises(Exception):
                    with c.begin():
                        c.execute(text(insert), {"s": sid, "a": MONEY, "k": "COMPLETE", "d": "{}"})
            result = run_alembic("upgrade", "head", database_url=bare_database)
            assert result.returncode == 0, result.stdout + result.stderr
            with eng.begin() as c:
                c.execute(text(insert), {"s": sid, "a": MONEY, "k": "COMPLETE", "d": "{}"})
                c.execute(text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details) "
                               "VALUES (:s, 'MONEY_RETURNED', 'WAIVER', gen_random_uuid(), '{CORRECTED}', "
                               "'{\"reason\": \"kept\"}')"), {"s": sid})
            with eng.connect() as c:
                with pytest.raises(Exception):  # a waiver is still only for the two returns
                    with c.begin():
                        c.execute(text(insert), {"s": sid, "a": "QUEUE", "k": "WAIVER", "d": '{"reason": "x"}'})
        finally:
            eng.dispose()

"""Final review, Part 1: the three things that would be worst to get wrong, proven against real PostgreSQL.

  A. The queue position counter. Not "positions came out right in a test run" but: a second writer genuinely WAITS on
     the counter row until the first commits (so position order can only be commit order), a rollback leaves no hole,
     a crowd of committing and rolling-back writers still gives 1..N, and Queue confirms racing the Stage Controller's
     DISPLAY NEXT / COMPLETE neither deadlock nor reorder anyone.
  B. Corrections. Every correction path is driven through the real HTTP API, and the DATABASE is asked afterwards:
     every row that existed before is byte-for-byte the same physical tuple (xmin, ctid, content hash), exactly one new
     row exists, and it points at the original. Every refusal writes nothing at all. The reason is refused server-side
     and by the schema's own CHECK constraints. Direct UPDATE / DELETE / TRUNCATE are refused by the triggers.
  C. Role guards on every /admin route (including the ones registered in main.py, which the console's own route-table
     test does not see), and the master-pack export: Admin only, audited, and its temporary file deleted.

Expected answers are raw SQL and hand-written literals, never taken from the code under test.
"""
import json
import random
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backend import users as users_svc
from backend.main import create_app
from tests.conftest import TEST_DB_URL
from tests.test_auth import PASSWORD, RESTRICT_VIOLATION, api_login, new_client
from tests.test_schema import REPO_ROOT
from tests.test_station_engine import (  # noqa: F401
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    make_student,
    operator,
    world,
)

CHECK_VIOLATION = "23514"


@pytest.fixture(scope="module")
def student_pool(engine):
    """Seed 40 students with tokens for the review tests."""
    return [make_student(engine, token=True) for _ in range(40)]


@pytest.fixture(autouse=True)
def clean_slate(engine):
    with engine.begin() as c:
        c.execute(text("SET session_replication_role = replica"))
        c.execute(text("TRUNCATE audit_log, scan_log, exceptions, activity_events, queue CASCADE"))
        c.execute(text("UPDATE counters SET value = 0 WHERE name = 'queue_position'"))
        c.execute(text("SET session_replication_role = origin"))
        c.execute(text("SELECT set_config('app.stage_controller', 'on', true)"))
        c.execute(text("UPDATE stage_state SET current_student_id = NULL, display_student_id = NULL, "
                       "previous_student_id = NULL, controller_session_id = NULL, controller_station_id = NULL, "
                       "controller_since = NULL WHERE id = 1"))
    yield


def s_id(pool, i):
    return pool[i - 1].id


def s_token(pool, i):
    return pool[i - 1].token


def raw_event(engine, pool, i, activity, *, kind="COMPLETE", cycle=1, corrects=None, details=None):
    """One event row inserted as plain SQL. Returns its event_id."""
    if details is None:
        details = {} if kind == "COMPLETE" else {"reason": "seeded"}
    with engine.begin() as c:
        return c.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, "
                 "completion_cycle, corrects_event_id) VALUES (:s, :a, :k, gen_random_uuid(), CAST(:f AS text[]), "
                 "CAST(:d AS jsonb), :c, :x) RETURNING event_id"),
            {"s": s_id(pool, i), "a": activity, "k": kind,
             "f": ["CORRECTED"] if kind in ("WAIVER", "REVERSAL") else [],
             "d": json.dumps(details), "c": cycle, "x": corrects},
        ).scalar_one().__str__()


def seat_ready(engine, pool, i):
    """Student i registered, robe given and seated: ready for the Queue."""
    for activity in ("REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED", "SEATING"):
        raw_event(engine, pool, i, activity)


def q_db(engine, sql, **params):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings()]


def scalar_db(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()


# =============================================================================================================
# A. THE QUEUE POSITION COUNTER
# =============================================================================================================
class TestQueueCounter:
    def _engine(self):
        return create_engine(TEST_DB_URL, pool_size=60, max_overflow=0)

    def test_a_second_writer_waits_for_the_first_to_commit_and_then_takes_the_next_number(self, engine, student_pool):
        eng = self._engine()
        a = eng.connect()
        txn = a.begin()
        first = a.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                          {"s": s_id(student_pool, 1)}).scalar_one()
        done, got = threading.Event(), {}

        def second():
            with eng.begin() as c:
                got["pos"] = c.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                                       {"s": s_id(student_pool, 2)}).scalar_one()
            got["finished_at"] = time.monotonic()
            done.set()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.8)
        assert not done.is_set(), "the second writer took a number while the first was still uncommitted: order would not be commit order"
        with eng.connect() as watcher:
            waiting = watcher.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")).scalar_one()
        assert waiting >= 1
        committed_at = time.monotonic()
        txn.commit()
        a.close()
        assert done.wait(10)
        t.join()
        assert got["finished_at"] >= committed_at and got["pos"] == first + 1
        eng.dispose()

    def test_a_rolled_back_writer_leaves_no_hole(self, engine, student_pool):
        eng = self._engine()
        a = eng.connect()
        txn = a.begin()
        burned = a.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                           {"s": s_id(student_pool, 1)}).scalar_one()
        got = {}

        def second():
            with eng.begin() as c:
                got["pos"] = c.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                                       {"s": s_id(student_pool, 2)}).scalar_one()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.5)
        txn.rollback()
        a.close()
        t.join(10)
        assert got["pos"] == burned == 1
        assert q_db(engine, "SELECT queue_position FROM queue ORDER BY 1") == [{"queue_position": 1}]
        eng.dispose()

    def test_a_crowd_of_committing_and_rolling_back_writers_still_gives_one_to_n(self, engine, student_pool):
        eng = self._engine()
        rng = random.Random(7)
        plan = [(i + 1, rng.random() < 0.3, rng.uniform(0, 0.03)) for i in range(36)]

        def work(item):
            i, rollback, hold = item
            conn = eng.connect()
            txn = conn.begin()
            pos = conn.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                               {"s": s_id(student_pool, i)}).scalar_one()
            time.sleep(hold)
            (txn.rollback if rollback else txn.commit)()
            conn.close()
            return None if rollback else pos

        with ThreadPoolExecutor(max_workers=36) as pool:
            results = list(pool.map(work, plan))
        committed = sorted(p for p in results if p is not None)
        assert committed == list(range(1, len(committed) + 1)), committed
        assert [r["queue_position"] for r in q_db(engine, "SELECT queue_position FROM queue ORDER BY 1")] == committed
        eng.dispose()

    def test_queue_confirms_racing_the_stage_controller_do_not_deadlock_or_reorder_anyone(self, engine, apps, world, student_pool):
        n = 24
        for i in range(1, n + 1):
            seat_ready(engine, student_pool, i)
        queue_op = operator(apps, world, "QUEUE")
        queue_token = queue_op.cookies.get("session")
        stage = operator(apps, world, "STAGE")
        stage_token = stage.cookies.get("session")

        def post(path, bearer, **body):
            client = new_client(apps)
            client.headers["Authorization"] = f"Bearer {bearer}"
            r = client.post(path, json=body)
            return r.status_code, r.json()

        assert post("/stage/control", stage_token, station_id="STG-01")[0] == 200
        problems, completed = [], []
        stop = threading.Event()

        def confirm_student(i):
            status, body = post("/confirm", queue_token, activity="QUEUE", token=s_token(student_pool, i + 1))
            if status != 200 or body.get("result") != "CONFIRMED":
                problems.append(("confirm", i + 1, status, body))

        def stage_loop():
            # NEXT (redesign R2): records the degree for whoever is on stage and shows the next one.
            on_stage = None
            while len(completed) < n and not stop.is_set():
                status, body = post("/stage/next", stage_token, station_id="STG-01", expect_current=on_stage)
                if status == 409 and body["detail"]["code"] == "QUEUE_EMPTY":
                    time.sleep(0.01)
                    continue
                if status != 200 or body.get("changed") is False:
                    problems.append(("next", status, body))
                    return
                if on_stage is not None:
                    completed.append(1)
                current = body["state"]["current"]
                on_stage = current["student_id"] if current else None

        stager = threading.Thread(target=stage_loop)
        stager.start()
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(confirm_student, range(n)))
        stager.join(60)
        stop.set()
        assert not problems, problems[:3]
        assert len(completed) == n
        positions = [r["queue_position"] for r in q_db(engine, "SELECT queue_position FROM queue ORDER BY 1")]
        assert positions == list(range(1, n + 1))
        order = [r["queue_position"] for r in q_db(
            engine, "SELECT q.queue_position FROM activity_events e JOIN queue q ON q.student_id = e.student_id "
                   "WHERE e.activity = 'STAGE' AND e.kind = 'COMPLETE' ORDER BY e.server_time, e.event_id")]
        assert order == sorted(order) == list(range(1, n + 1)), order


# =============================================================================================================
# B. CORRECTIONS, CHECKED IN THE DATABASE
# =============================================================================================================
def snapshot(engine):
    """Every history row as PostgreSQL physically holds it."""
    return {
        "events": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in q_db(
            engine, "SELECT event_id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM activity_events t")},
        "audit": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in q_db(
            engine, "SELECT id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM audit_log t")},
        "scans": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in q_db(
            engine, "SELECT id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM scan_log t")},
    }


def assert_history_untouched(before, after):
    for table in ("events", "audit", "scans"):
        changed = {k for k, v in before[table].items() if after[table].get(k) != v}
        assert not changed, f"{table}: rows that existed before were modified or removed: {sorted(changed)[:3]}"


def student_journey(engine, apps, world, student_pool, i=1):
    """Student i: Reporting seeded, then robe, seating, queue and stage through the REAL API. Returns event ids."""
    raw_event(engine, student_pool, i, "REGISTRATION")
    for activity in ("THOBE_ALLOCATION", "SEATING", "QUEUE"):
        op = operator(apps, world, activity)
        assert confirm(op, activity, token=s_token(student_pool, i)).json()["result"] == "CONFIRMED", activity
    stage = operator(apps, world, "STAGE")
    assert stage.post("/stage/control", json={"station_id": "STG-01"}).status_code == 200
    shown = stage.post("/stage/next", json={"station_id": "STG-01", "expect_current": None})
    assert shown.status_code == 200
    on_stage = shown.json()["state"]["current"]["student_id"]
    assert stage.post("/stage/next", json={"station_id": "STG-01", "expect_current": on_stage}).status_code == 200
    return {r["activity"]: str(r["event_id"]) for r in q_db(
        engine, "SELECT activity, event_id FROM activity_events WHERE student_id = :s AND kind = 'COMPLETE'",
        s=s_id(student_pool, i))}


class TestCorrectionsAtTheDatabase:
    def reverse(self, adm, event_id, reason="rehearsal: wrong student"):
        return adm.post("/admin/api/corrections/reverse", json={"event_id": event_id, "reason": reason})

    def test_every_reversal_writes_one_new_row_and_leaves_every_old_row_physically_untouched(self, engine, apps, world, student_pool):
        ids = student_journey(engine, apps, world, student_pool, 1)
        adm = admin(apps)
        assert set(ids) == {"REGISTRATION", "THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"}
        for activity in ("SEATING", "QUEUE", "STAGE"):
            before, count = snapshot(engine), scalar_db(engine, "SELECT count(*) FROM activity_events")
            response = self.reverse(adm, ids[activity])
            assert response.status_code == 200, response.text
            after = snapshot(engine)
            assert_history_untouched(before, after)
            assert scalar_db(engine, "SELECT count(*) FROM activity_events") == count + 1
            new = q_db(engine, "SELECT * FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=response.json()["correction_event_id"])[0]
            assert (new["kind"], new["activity"], str(new["corrects_event_id"]), new["completion_cycle"]) == ("REVERSAL", activity, ids[activity], 1)
            assert "CORRECTED" in new["flags"] and new["details"]["reason"] == "rehearsal: wrong student"
            original = q_db(engine, "SELECT kind, flags FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=ids[activity])[0]
            assert original["kind"] == "COMPLETE" and "CORRECTED" not in original["flags"]
            audit = q_db(engine, "SELECT action, reason, corrected_by FROM audit_log WHERE event_id = CAST(:e AS uuid)", e=new["event_id"])[0]
            assert audit["action"] == "ADMIN_REVERSAL" and audit["reason"] == "rehearsal: wrong student" and audit["corrected_by"] is not None

    def test_a_waiver_and_the_reversal_of_a_waiver_are_new_rows_too(self, engine, apps, world, student_pool):
        raw_event(engine, student_pool, 2, "REGISTRATION")
        for activity in ("THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"):
            raw_event(engine, student_pool, 2, activity)
        adm = admin(apps)
        before = snapshot(engine)
        waived = adm.post("/admin/api/corrections/waive-return", json={"student_id": str(s_id(student_pool, 2)), "reason": "lost robe"})
        assert waived.status_code == 200, waived.text
        mid = snapshot(engine)
        assert_history_untouched(before, mid)
        assert len(mid["events"]) == len(before["events"]) + 1
        wid = waived.json()["correction_event_id"]
        assert q_db(engine, "SELECT kind, flags FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=wid)[0]["kind"] == "WAIVER"
        undone = self.reverse(adm, wid, "waived the wrong student")
        assert undone.status_code == 200, undone.text
        after = snapshot(engine)
        assert_history_untouched(mid, after)
        assert len(after["events"]) == len(mid["events"]) + 1
        assert scalar_db(engine, "SELECT kind FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=wid) == "WAIVER"

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
    def test_a_blank_reason_is_refused_by_the_server_and_writes_nothing(self, engine, apps, world, student_pool, reason):
        ids = student_journey(engine, apps, world, student_pool, 1)
        adm = admin(apps)
        before = snapshot(engine)
        assert self.reverse(adm, ids["SEATING"], reason).status_code == 400
        assert adm.post("/admin/api/corrections/reverse", json={"event_id": ids["SEATING"]}).status_code == 400
        assert snapshot(engine) == before

    def test_every_other_refusal_also_writes_nothing_at_all(self, engine, apps, world, student_pool):
        ids = student_journey(engine, apps, world, student_pool, 1)
        skip = raw_event(engine, student_pool, 3, "STAGE", kind="SKIP")
        adm = admin(apps)
        assert self.reverse(adm, ids["SEATING"]).status_code == 200
        operator_seating = operator(apps, world, "SEATING")
        before = snapshot(engine)
        refusals = [
            (self.reverse(adm, ids["SEATING"]), 409),                    # already reversed
            (self.reverse(adm, "not-a-uuid"), 404),
            (self.reverse(adm, str(uuid.uuid4())), 404),
            (self.reverse(adm, skip), 409),                              # a SKIP is not a completion
            (self.reverse(adm, ids["QUEUE"], "x" * 501), 400),
            (operator_seating.post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "operators must not"}), 403),
            (operator_seating.post("/admin/api/corrections/waive-return", json={"student_id": str(s_id(student_pool, 1)), "reason": "no"}), 403),
            (new_client(apps).post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "anon"}), 401),
        ]
        for response, expected in refusals:
            assert response.status_code == expected, (expected, response.status_code, response.text)
        assert snapshot(engine) == before

    def test_the_schema_itself_refuses_history_edits_and_a_correction_without_a_reason(self, engine, apps, world, student_pool):
        ids = student_journey(engine, apps, world, student_pool, 1)
        before = snapshot(engine)

        def refused(sql, code, **params):
            with pytest.raises(DBAPIError) as caught:
                with engine.begin() as c:
                    c.execute(text(sql), params)
            assert caught.value.orig.pgcode == code, (sql, caught.value.orig.pgcode)

        refused("UPDATE activity_events SET flags = ARRAY['CORRECTED'] WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("UPDATE activity_events SET details = '{}' WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("DELETE FROM activity_events WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("TRUNCATE activity_events", RESTRICT_VIOLATION)
        refused("UPDATE audit_log SET reason = 'edited'", RESTRICT_VIOLATION)
        refused("DELETE FROM scan_log", RESTRICT_VIOLATION)
        insert = ("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details, completion_cycle, corrects_event_id) "
                  "VALUES (:s, 'SEATING', 'REVERSAL', gen_random_uuid(), ARRAY['CORRECTED'], CAST(:d AS jsonb), 1, CAST(:e AS uuid))")
        refused(insert, CHECK_VIOLATION, s=s_id(student_pool, 1), d='{}', e=ids["SEATING"])
        refused(insert, CHECK_VIOLATION, s=s_id(student_pool, 1), d='{"reason": "   "}', e=ids["SEATING"])
        refused(insert, CHECK_VIOLATION, s=s_id(student_pool, 2), d='{"reason": "x"}', e=ids["SEATING"])
        assert snapshot(engine) == before

    def test_two_admins_reversing_the_same_record_at_once_leave_exactly_one_reversal(self, engine, apps, world, student_pool):
        ids = student_journey(engine, apps, world, student_pool, 1)
        sessions = []
        for n in range(6):
            client = new_client(apps)
            with engine.begin() as c:
                users_svc.create_user(c, username=f"deputy{n}", password=PASSWORD, role="DEPUTY_ADMIN")
            assert api_login(client, f"deputy{n}").status_code == 200
            sessions.append(client)
        results = list(ThreadPoolExecutor(max_workers=6).map(
            lambda c: c.post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "race"}).status_code, sessions))
        assert sorted(results) == [200, 409, 409, 409, 409, 409], results
        assert scalar_db(engine, "SELECT count(*) FROM activity_events WHERE kind = 'REVERSAL' AND activity = 'QUEUE'") == 1

    def test_nothing_in_backend_ever_updates_or_deletes_activity_events(self):
        pattern = re.compile(r"\b(UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+activity_events\b", re.IGNORECASE)
        offenders = [str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "backend").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
        assert offenders == []


# =============================================================================================================
# C. ROLE GUARDS ON EVERY /admin ROUTE, AND THE MASTER-PACK EXPORT
# =============================================================================================================
class TestGuardsAndExports:
    def _admin_routes(self, app):
        """Every (method, path) under /admin, from the app's OpenAPI schema."""
        return [(method.upper(), path) for path, item in app.openapi()["paths"].items() if path.startswith("/admin")
                for method in item if method.lower() in ("get", "post", "put", "patch", "delete")]

    def test_every_admin_route_refuses_an_operator_and_a_signed_out_visitor(self, apps, world):
        routes = self._admin_routes(apps)
        assert ("POST", "/admin/import/commit") in routes and ("GET", "/admin/master-pack/export") in routes
        assert ("POST", "/admin/api/corrections/reverse") in routes and len(routes) > 30
        operator_seating = operator(apps, world, "SEATING")
        anon = new_client(apps)
        for method, path in routes:
            url = re.sub(r"\{[^}]+\}", "1", path)
            for who, expected in ((operator_seating, 403), (anon, 401)):
                r = who.request(method, url, follow_redirects=False)
                assert r.status_code == expected, f"{method} {path} as {'operator' if who is operator_seating else 'anonymous'}: {r.status_code}"

    def test_the_deputy_admin_passes_the_same_guards_as_the_admin(self, engine, apps):
        with engine.begin() as c:
            users_svc.create_user(c, username="deputy", password=PASSWORD, role="DEPUTY_ADMIN")
        deputy = new_client(apps)
        assert api_login(deputy, "deputy").status_code == 200
        for path in ("/admin/api/dashboard", "/admin/api/reports", "/admin/api/exceptions", "/admin/api/audit"):
            assert deputy.get(path).status_code == 200, path

    def test_the_master_pack_export_is_admin_only_audited_and_leaves_no_temporary_file(self, engine, apps, world):
        operator_seating = operator(apps, world, "SEATING")
        adm = admin(apps)
        tmp = Path(tempfile.gettempdir())
        zips_before = {p.name for p in tmp.glob("*.zip")}
        assert operator_seating.get("/admin/master-pack/export").status_code == 403
        assert new_client(apps).get("/admin/master-pack/export").status_code == 401
        assert scalar_db(engine, "SELECT count(*) FROM audit_log WHERE action = 'EXPORT_MASTER_PACK'") == 0
        response = adm.get("/admin/master-pack/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip" and response.content[:2] == b"PK"
        row = q_db(engine, "SELECT operator_id::text AS op FROM audit_log WHERE action = 'EXPORT_MASTER_PACK'")
        assert len(row) == 1 and row[0]["op"] == str(world.admin_id)
        assert {p.name for p in tmp.glob("*.zip")} - zips_before == set()

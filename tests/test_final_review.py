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
from backend.security.ownership import ACTIVITY_OWNER as OWNER_OF
from tests.sync_support import Stack
from tests.test_auth import PASSWORD, RESTRICT_VIOLATION, api_login, new_client
from tests.test_schema import REPO_ROOT

CHECK_VIOLATION = "23514"


@pytest.fixture(scope="module")
def stack():
    s = Stack("review", students=40)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_slate(stack):
    stack.reset()
    yield


def raw_event(stack, venue, i, activity, *, kind="COMPLETE", cycle=1, corrects=None, details=None):
    """One event row inserted as plain SQL (as if earlier, or replicated in). Returns its event_id."""
    if details is None:
        details = {} if kind == "COMPLETE" else {"reason": "seeded"}
    with stack.engine(venue).begin() as c:
        return c.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details, "
                 "completion_cycle, corrects_event_id) VALUES (:s, :a, :k, :v, :st, gen_random_uuid(), CAST(:f AS text[]), "
                 "CAST(:d AS jsonb), :c, :x) RETURNING event_id"),
            {"s": stack.student_id(i), "a": activity, "k": kind, "v": OWNER_OF[activity],
             "st": None if kind in ("WAIVER", "REVERSAL") else "SEED-1", "f": ["CORRECTED"] if kind in ("WAIVER", "REVERSAL") else [],
             "d": json.dumps(details), "c": cycle, "x": corrects},
        ).scalar_one().__str__()


def seat_ready(stack, i):
    """Student i at the Stadium, registered (College), thobe given and seated: ready for the Queue."""
    for activity in ("REGISTRATION", "THOBE_ALLOCATION", "SEATING"):
        raw_event(stack, "stadium", i, activity)


# =============================================================================================================
# A. THE QUEUE POSITION COUNTER
# =============================================================================================================
class TestQueueCounter:
    def _engine(self, stack):
        return create_engine(stack.urls["stadium"], pool_size=60, max_overflow=0)

    def test_a_second_writer_waits_for_the_first_to_commit_and_then_takes_the_next_number(self, stack):
        eng = self._engine(stack)
        a = eng.connect()
        txn = a.begin()
        first = a.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"), {"s": stack.student_id(1)}).scalar_one()
        done, got = threading.Event(), {}

        def second():
            with eng.begin() as c:
                got["pos"] = c.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                                       {"s": stack.student_id(2)}).scalar_one()
            got["finished_at"] = time.monotonic()
            done.set()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.8)
        assert not done.is_set(), "the second writer took a number while the first was still uncommitted: order would not be commit order"
        with eng.connect() as watcher:  # and it is genuinely waiting on a lock, not merely slow
            waiting = watcher.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")).scalar_one()
        assert waiting >= 1
        committed_at = time.monotonic()
        txn.commit()
        a.close()
        assert done.wait(10)
        t.join()
        assert got["finished_at"] >= committed_at and got["pos"] == first + 1
        eng.dispose()

    def test_a_rolled_back_writer_leaves_no_hole(self, stack):
        eng = self._engine(stack)
        a = eng.connect()
        txn = a.begin()
        burned = a.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"), {"s": stack.student_id(1)}).scalar_one()
        got = {}

        def second():
            with eng.begin() as c:
                got["pos"] = c.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                                       {"s": stack.student_id(2)}).scalar_one()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.5)
        txn.rollback()
        a.close()
        t.join(10)
        assert got["pos"] == burned == 1  # the number the loser never really used is handed out again
        assert stack.q("stadium", "SELECT queue_position FROM queue ORDER BY 1") == [{"queue_position": 1}]
        eng.dispose()

    def test_a_crowd_of_committing_and_rolling_back_writers_still_gives_one_to_n(self, stack):
        eng = self._engine(stack)
        rng = random.Random(7)
        plan = [(i + 1, rng.random() < 0.3, rng.uniform(0, 0.03)) for i in range(36)]  # (student, rolls back?, hold time)

        def work(item):
            i, rollback, hold = item
            conn = eng.connect()
            txn = conn.begin()
            pos = conn.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"), {"s": stack.student_id(i)}).scalar_one()
            time.sleep(hold)
            (txn.rollback if rollback else txn.commit)()
            conn.close()
            return None if rollback else pos

        with ThreadPoolExecutor(max_workers=36) as pool:
            results = list(pool.map(work, plan))
        committed = sorted(p for p in results if p is not None)
        assert committed == list(range(1, len(committed) + 1)), committed                      # unique and gap-free
        assert [r["queue_position"] for r in stack.q("stadium", "SELECT queue_position FROM queue ORDER BY 1")] == committed
        eng.dispose()

    def test_queue_confirms_racing_the_stage_controller_do_not_deadlock_or_reorder_anyone(self, stack):
        n = 24
        for i in range(1, n + 1):
            seat_ready(stack, i)
        stations = ["QUE-01", "QUE-02", "QUE-03"]
        tokens = {s: stack.operator("stadium", "QUEUE", s).cookies.get("session") for s in stations}
        stage = stack.operator("stadium", "STAGE", "STG-01")
        stage_token = stage.cookies.get("session")
        app = stack.app("stadium")

        def post(path, bearer, **body):
            client = new_client(app)
            client.headers["Authorization"] = f"Bearer {bearer}"
            r = client.post(path, json=body)
            return r.status_code, r.json()

        assert post("/stage/control", stage_token, station_id="STG-01")[0] == 200
        problems, completed = [], []
        stop = threading.Event()

        def confirm(i):
            status, body = post("/confirm", tokens[stations[i % 3]], station_id=stations[i % 3], token=stack.token(i + 1))
            if status != 200 or body.get("result") != "CONFIRMED":
                problems.append(("confirm", i + 1, status, body))

        def stage_loop():
            while len(completed) < n and not stop.is_set():
                status, body = post("/stage/display-next", stage_token, station_id="STG-01")
                if status == 409 and body["detail"]["code"] == "QUEUE_EMPTY":
                    time.sleep(0.01)
                    continue
                if status != 200:
                    problems.append(("display-next", status, body))
                    return
                status, body = post("/stage/complete", stage_token, station_id="STG-01")
                if status != 200:
                    problems.append(("complete", status, body))
                    return
                completed.append(1)

        stager = threading.Thread(target=stage_loop)
        stager.start()
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(confirm, range(n)))
        stager.join(60)
        stop.set()
        assert not problems, problems[:3]
        assert len(completed) == n
        positions = [r["queue_position"] for r in stack.q("stadium", "SELECT queue_position FROM queue ORDER BY 1")]
        assert positions == list(range(1, n + 1))
        # Stage completed everyone, in exactly queue order: each Stage event's student has a higher position than the last.
        order = [r["queue_position"] for r in stack.q(
            "stadium", "SELECT q.queue_position FROM activity_events e JOIN queue q ON q.student_id = e.student_id "
                       "WHERE e.activity = 'STAGE' AND e.kind = 'COMPLETE' ORDER BY e.venue_seq")]
        assert order == sorted(order) == list(range(1, n + 1)), order


# =============================================================================================================
# B. CORRECTIONS, CHECKED IN THE DATABASE
# =============================================================================================================
def snapshot(stack, venue):
    """Every history row as PostgreSQL physically holds it. An UPDATE of any kind (even one that changes nothing)
    creates a new tuple version, so xmin / ctid would move: stronger than comparing columns."""
    return {
        "events": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in stack.q(
            venue, "SELECT event_id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM activity_events t")},
        "audit": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in stack.q(
            venue, "SELECT id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM audit_log t")},
        "scans": {r["k"]: (r["xmin"], r["ctid"], r["h"]) for r in stack.q(
            venue, "SELECT id::text AS k, xmin::text AS xmin, ctid::text AS ctid, md5(t::text) AS h FROM scan_log t")},
        "outbox": stack.scalar(venue, "SELECT count(*) FROM outbox"),
    }


def assert_history_untouched(before, after):
    for table in ("events", "audit", "scans"):
        changed = {k for k, v in before[table].items() if after[table].get(k) != v}
        assert not changed, f"{table}: rows that existed before were modified or removed: {sorted(changed)[:3]}"


def stadium_journey(stack, i=1):
    """Student i: Registration seeded, then thobe, seating, queue and stage through the REAL API. Returns event ids."""
    raw_event(stack, "stadium", i, "REGISTRATION")
    for activity, station in (("THOBE_ALLOCATION", "THO-01"), ("SEATING", "SEA-01"), ("QUEUE", "QUE-01")):
        op = stack.operator("stadium", activity, station)
        assert stack.confirm(op, station, i)["result"] == "CONFIRMED", activity
    stage = stack.operator("stadium", "STAGE", "STG-01")
    assert stage.post("/stage/control", json={"station_id": "STG-01"}).status_code == 200
    assert stage.post("/stage/display-next", json={"station_id": "STG-01"}).status_code == 200
    assert stage.post("/stage/complete", json={"station_id": "STG-01"}).status_code == 200
    return {r["activity"]: r["event_id"] for r in stack.events("stadium", f"student_id = '{stack.student_id(i)}' AND kind = 'COMPLETE'")}


class TestCorrectionsAtTheDatabase:
    def reverse(self, admin, event_id, reason="rehearsal: wrong student"):
        return admin.post("/admin/api/corrections/reverse", json={"event_id": event_id, "reason": reason})

    def test_every_reversal_writes_one_new_row_and_leaves_every_old_row_physically_untouched(self, stack):
        ids = stadium_journey(stack, 1)
        admin = stack.admin("stadium")
        assert set(ids) == {"REGISTRATION", "THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"}
        for activity in ("SEATING", "QUEUE", "STAGE"):  # each with later activities still recorded, and their queue side effects
            before, count = snapshot(stack, "stadium"), stack.scalar("stadium", "SELECT count(*) FROM activity_events")
            response = self.reverse(admin, ids[activity])
            assert response.status_code == 200, response.text
            after = snapshot(stack, "stadium")
            assert_history_untouched(before, after)
            assert stack.scalar("stadium", "SELECT count(*) FROM activity_events") == count + 1
            assert after["outbox"] == before["outbox"] + 1                               # and its outbox row, same transaction
            new = stack.q("stadium", "SELECT * FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=response.json()["correction_event_id"])[0]
            assert (new["kind"], new["activity"], str(new["corrects_event_id"]), new["completion_cycle"]) == ("REVERSAL", activity, ids[activity], 1)
            assert "CORRECTED" in new["flags"] and new["details"]["reason"] == "rehearsal: wrong student"
            original = stack.q("stadium", "SELECT kind, flags FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=ids[activity])[0]
            assert original["kind"] == "COMPLETE" and "CORRECTED" not in original["flags"]   # the original is not even flagged
            audit = stack.q("stadium", "SELECT action, reason, corrected_by FROM audit_log WHERE event_id = CAST(:e AS uuid)", e=new["event_id"])[0]
            assert audit["action"] == "ADMIN_REVERSAL" and audit["reason"] == "rehearsal: wrong student" and audit["corrected_by"] is not None

    def test_a_waiver_and_the_reversal_of_a_waiver_are_new_rows_too(self, stack):
        raw_event(stack, "hall", 2, "REGISTRATION")
        for activity in ("THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"):
            raw_event(stack, "hall", 2, activity)          # replicated-in history: the Hall did not write these
        admin = stack.admin("hall")
        before = snapshot(stack, "hall")
        waived = admin.post("/admin/api/corrections/waive-return", json={"student_id": str(stack.student_id(2)), "reason": "lost thobe"})
        assert waived.status_code == 200, waived.text
        mid = snapshot(stack, "hall")
        assert_history_untouched(before, mid)
        assert len(mid["events"]) == len(before["events"]) + 1
        wid = waived.json()["correction_event_id"]
        assert stack.q("hall", "SELECT kind, flags FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=wid)[0]["kind"] == "WAIVER"
        undone = self.reverse(admin, wid, "waived the wrong student")
        assert undone.status_code == 200, undone.text
        after = snapshot(stack, "hall")
        assert_history_untouched(mid, after)
        assert len(after["events"]) == len(mid["events"]) + 1
        assert stack.scalar("hall", "SELECT kind FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=wid) == "WAIVER"  # still there

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
    def test_a_blank_reason_is_refused_by_the_server_and_writes_nothing(self, stack, reason):
        ids = stadium_journey(stack, 1)
        admin = stack.admin("stadium")
        before = snapshot(stack, "stadium")
        assert self.reverse(admin, ids["SEATING"], reason).status_code == 400
        assert admin.post("/admin/api/corrections/reverse", json={"event_id": ids["SEATING"]}).status_code == 400  # field omitted entirely
        assert snapshot(stack, "stadium") == before

    def test_every_other_refusal_also_writes_nothing_at_all(self, stack):
        ids = stadium_journey(stack, 1)
        skip = raw_event(stack, "stadium", 3, "STAGE", kind="SKIP")
        hall_event = raw_event(stack, "stadium", 4, "THOBE_RETURN")        # a Hall record sitting in the Stadium's database
        admin = stack.admin("stadium")
        assert self.reverse(admin, ids["SEATING"]).status_code == 200      # so "already reversed" is testable
        operator = stack.operator("stadium", "SEATING", "SEA-02")           # (creating and signing in a user is itself audited)
        before = snapshot(stack, "stadium")
        refusals = [
            (self.reverse(admin, ids["SEATING"]), 409),                    # already reversed
            (self.reverse(admin, "not-a-uuid"), 404),
            (self.reverse(admin, str(uuid.uuid4())), 404),
            (self.reverse(admin, skip), 409),                              # a SKIP is not a completion
            (self.reverse(admin, hall_event), 409),                        # the Hall's activity: not corrected here
            (self.reverse(admin, ids["QUEUE"], "x" * 501), 400),
            (admin.post("/admin/api/corrections/waive-return", json={"student_id": str(stack.student_id(1)), "reason": "lost"}), 409),  # Hall only
            (operator.post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "operators must not"}), 403),
            (operator.post("/admin/api/corrections/waive-return", json={"student_id": str(stack.student_id(1)), "reason": "no"}), 403),
            (new_client(stack.app("stadium")).post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "anon"}), 401),
        ]
        for response, expected in refusals:
            assert response.status_code == expected, (expected, response.status_code, response.text)
        assert snapshot(stack, "stadium") == before

    def test_the_schema_itself_refuses_history_edits_and_a_correction_without_a_reason(self, stack):
        ids = stadium_journey(stack, 1)
        before = snapshot(stack, "stadium")
        eng = stack.engine("stadium")

        def refused(sql, code, **params):
            with pytest.raises(DBAPIError) as caught:
                with eng.begin() as c:
                    c.execute(text(sql), params)
            assert caught.value.orig.pgcode == code, (sql, caught.value.orig.pgcode)

        refused("UPDATE activity_events SET flags = ARRAY['CORRECTED'] WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("UPDATE activity_events SET details = '{}' WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("DELETE FROM activity_events WHERE event_id = CAST(:e AS uuid)", RESTRICT_VIOLATION, e=ids["SEATING"])
        refused("TRUNCATE activity_events", RESTRICT_VIOLATION)
        refused("UPDATE audit_log SET reason = 'edited'", RESTRICT_VIOLATION)
        refused("DELETE FROM scan_log", RESTRICT_VIOLATION)
        insert = ("INSERT INTO activity_events (student_id, activity, kind, venue_id, operator_id, flags, details, completion_cycle, corrects_event_id) "
                  "VALUES (:s, 'SEATING', 'REVERSAL', 'stadium', gen_random_uuid(), ARRAY['CORRECTED'], CAST(:d AS jsonb), 1, CAST(:e AS uuid))")
        refused(insert, CHECK_VIOLATION, s=stack.student_id(1), d='{}', e=ids["SEATING"])                     # no reason at all
        refused(insert, CHECK_VIOLATION, s=stack.student_id(1), d='{"reason": "   "}', e=ids["SEATING"])      # a blank reason
        refused(insert, CHECK_VIOLATION, s=stack.student_id(2), d='{"reason": "x"}', e=ids["SEATING"])        # points at another student's record
        assert snapshot(stack, "stadium") == before

    def test_two_admins_reversing_the_same_record_at_once_leave_exactly_one_reversal(self, stack):
        ids = stadium_journey(stack, 1)
        app = stack.app("stadium")
        sessions = []
        for n in range(6):
            client = new_client(app)
            with stack.engine("stadium").begin() as c:
                users_svc.create_user(c, username=f"deputy{n}", password=PASSWORD, role="DEPUTY_ADMIN")
            assert api_login(client, f"deputy{n}").status_code == 200
            sessions.append(client)
        results = list(ThreadPoolExecutor(max_workers=6).map(
            lambda c: c.post("/admin/api/corrections/reverse", json={"event_id": ids["QUEUE"], "reason": "race"}).status_code, sessions))
        assert sorted(results) == [200, 409, 409, 409, 409, 409], results
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE kind = 'REVERSAL' AND activity = 'QUEUE'") == 1

    def test_nothing_in_backend_ever_updates_or_deletes_activity_events(self):
        pattern = re.compile(r"\b(UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?)\s+activity_events\b", re.IGNORECASE)
        offenders = [str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "backend").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
        assert offenders == []


# =============================================================================================================
# C. ROLE GUARDS ON EVERY /admin ROUTE, AND THE MASTER-PACK EXPORT
# =============================================================================================================
class TestGuardsAndExports:
    def _admin_routes(self, app):
        """Every (method, path) under /admin, from the app's OpenAPI schema: this FastAPI wraps included routers, so
        app.routes does not list them, while the schema lists every route registered anywhere (console, web_admin, main)."""
        return [(method.upper(), path) for path, item in app.openapi()["paths"].items() if path.startswith("/admin")
                for method in item if method.lower() in ("get", "post", "put", "patch", "delete")]

    def test_every_admin_route_refuses_an_operator_and_a_signed_out_visitor(self, stack):
        app = stack.app("stadium")
        routes = self._admin_routes(app)
        # The console (backend/admin) AND the routes main.py registers itself: import, photos, snapshot, master pack.
        assert ("POST", "/admin/import/commit") in routes and ("GET", "/admin/master-pack/export") in routes
        assert ("POST", "/admin/api/corrections/reverse") in routes and len(routes) > 30
        operator = stack.operator("stadium", "SEATING", "SEA-01")
        anon = new_client(app)
        for method, path in routes:
            url = re.sub(r"\{[^}]+\}", "1", path)
            for who, expected in ((operator, 403), (anon, 401)):
                r = who.request(method, url, follow_redirects=False)
                assert r.status_code == expected, f"{method} {path} as {'operator' if who is operator else 'anonymous'}: {r.status_code}"

    def test_the_deputy_admin_passes_the_same_guards_as_the_admin(self, stack):
        with stack.engine("stadium").begin() as c:
            users_svc.create_user(c, username="deputy", password=PASSWORD, role="DEPUTY_ADMIN")
        deputy = new_client(stack.app("stadium"))
        assert api_login(deputy, "deputy").status_code == 200
        for path in ("/admin/api/dashboard", "/admin/api/reports", "/admin/api/exceptions", "/admin/api/audit"):
            assert deputy.get(path).status_code == 200, path

    def test_the_master_pack_export_is_admin_only_audited_and_leaves_no_temporary_file(self, stack):
        app = stack.app("stadium")
        operator = stack.operator("stadium", "SEATING", "SEA-01")
        admin = stack.admin("stadium")
        tmp = Path(tempfile.gettempdir())
        zips_before = {p.name for p in tmp.glob("*.zip")}
        assert operator.get("/admin/master-pack/export").status_code == 403
        assert new_client(app).get("/admin/master-pack/export").status_code == 401
        assert stack.scalar("stadium", "SELECT count(*) FROM audit_log WHERE action = 'EXPORT_MASTER_PACK'") == 0  # refused: nothing logged
        response = admin.get("/admin/master-pack/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip" and response.content[:2] == b"PK"
        row = stack.q("stadium", "SELECT operator_id::text AS op, venue_id FROM audit_log WHERE action = 'EXPORT_MASTER_PACK'")
        assert row == [{"op": str(stack.admin_ids["stadium"]), "venue_id": "stadium"}]
        assert {p.name for p in tmp.glob("*.zip")} - zips_before == set()       # the pack holds every QR token: it must not linger

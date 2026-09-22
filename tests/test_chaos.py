"""Final review, Part 2: the few failures that would be catastrophic, against real PostgreSQL, real sockets and real processes.

  1. A sustained cut. The Stadium loses its link to central (its worker meets a genuinely refused connection) for longer
     than the freshness window while its operators keep scanning, College keeps registering and the Hall keeps working.
     After reconnect every database holds the same events, every outbox is drained, and the ONLY exceptions that ever
     existed are the two PROVISIONAL ones this scenario plants, both closed by the system.
  2. Killing the application process. A real `uvicorn` OS process is killed (TerminateProcess: no cleanup runs) while
     confirms are in flight and while its sync worker is mid-drain, then restarted, several times. Afterwards, for every
     confirmed action, the event, its outbox row, its audit row and its scan-log row are all present, or none is, and
     the queue numbering has no hole. One test kills the process INSIDE an open confirm transaction, on purpose.

Time is real: freshness is made to expire by waiting, not by editing a timestamp. Expected numbers are written out by hand.
"""
import os
import random
import subprocess
import sys
import threading
import time

import httpx
import pytest
from sqlalchemy import text

from backend import stations as stations_svc
from backend import users as users_svc
from tests.sync_support import VENUES, Stack, free_port, status_rows
from tests.test_auth import PASSWORD
from tests.test_final_review import raw_event, seat_ready
from tests.test_schema import REPO_ROOT


@pytest.fixture(scope="module")
def stack():
    s = Stack("chaos", students=60)
    yield s
    s.close()


def event_ids(stack, venue):
    return {r["event_id"] for r in stack.events(venue)}


# =============================================================================================================
# 1. A SUSTAINED CUT OF ONE VENUE
# =============================================================================================================
def test_a_sustained_stadium_cut_loses_nothing_and_leaves_only_the_two_planned_provisional_exceptions(stack):
    stack.reset()
    stack.start_central()
    for v in VENUES:  # a 2-second freshness window, so "sustained" is a few real seconds rather than real minutes
        with stack.engine(v).begin() as c:
            c.execute(text("UPDATE settings SET freshness_window_seconds = 2 WHERE id = 1"))
    workers = {v: stack.worker(v) for v in VENUES}
    dead_central = f"http://127.0.0.1:{free_port()}"            # nothing listens here: a real "connection refused"
    stadium_cut = stack.worker("stadium", central_url=dead_central)

    def sync(*which, rounds=1):
        for _ in range(rounds):
            for w in which:
                w.cycle()

    # --- normal running: College registers students 1..8; everyone syncs; all links are healthy ------------------
    reg = stack.operator("college", "REGISTRATION", "REG-01")
    for i in range(1, 9):
        assert stack.confirm(reg, "REG-01", i)["result"] == "CONFIRMED"
    sync(*workers.values(), rounds=3)
    assert len(stack.events("stadium", "activity = 'REGISTRATION'")) == 8

    # --- THE CUT: the Stadium can no longer reach central ---------------------------------------------------------
    tho = stack.operator("stadium", "THOBE_ALLOCATION", "THO-01")
    sea = stack.operator("stadium", "SEATING", "SEA-01")
    que = stack.operator("stadium", "QUEUE", "QUE-01")
    stage = stack.operator("stadium", "STAGE", "STG-01")
    cut_started = time.monotonic()
    assert stadium_cut.cycle().ok is False                       # it really is cut off

    for i in range(1, 7):                                        # scans continue locally, as if nothing had happened
        assert stack.confirm(tho, "THO-01", i)["result"] == "CONFIRMED"
    for i in range(1, 6):
        assert stack.confirm(sea, "SEA-01", i)["result"] == "CONFIRMED"
    for i in range(1, 5):
        assert stack.confirm(que, "QUE-01", i)["result"] == "CONFIRMED"
    assert stage.post("/stage/control", json={"station_id": "STG-01"}).status_code == 200
    for _ in range(2):                                           # students 1 and 2 cross the stage, offline
        assert stage.post("/stage/display-next", json={"station_id": "STG-01"}).status_code == 200
        assert stage.post("/stage/complete", json={"station_id": "STG-01"}).status_code == 200

    for i in range(9, 13):                                       # College (still online) registers four more
        assert stack.confirm(reg, "REG-01", i)["result"] == "CONFIRMED"

    while time.monotonic() - cut_started < 3.5:                  # sustained: longer than the 2 s window, all the while trying
        sync(workers["college"], workers["hall"], stadium_cut)
        time.sleep(0.4)
    assert stadium_cut.cycle().ok is False

    # Now stale: the Stadium accepts a student whose Registration it has not received (PROVISIONAL, normal for the operator) ...
    assert stack.confirm(tho, "THO-01", 9)["result"] == "CONFIRMED"
    # ... and the Hall, whose view of the cut Stadium is stale too, accepts student 1's return and lunch.
    ret = stack.operator("hall", "THOBE_RETURN", "RET-01")
    lun = stack.operator("hall", "LUNCH", "LUN-01")
    sync(workers["hall"])
    assert stack.confirm(ret, "RET-01", 1)["result"] == "CONFIRMED"
    assert stack.confirm(lun, "LUN-01", 1)["result"] == "CONFIRMED"

    stadium_events = stack.events("stadium", "venue_id = 'stadium'")
    assert len(stadium_events) == 6 + 1 + 5 + 4 + 2 == 18       # thobe (6+1 provisional), seating, queue, stage: by hand
    health = stack.admin("stadium").get("/admin/api/sync").json()  # what the Admin sees at the Stadium during the cut
    assert health["state"] == "OFFLINE" and health["pending_records"] == 18, health
    assert stack.scalar("central", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 0   # none of it has left

    # --- RECONNECT: the real workers again; nothing is done by hand ------------------------------------------------
    reconnected = time.monotonic()
    for _ in range(40):
        sync(*workers.values())
        if all(stack.scalar(v, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0 for v in VENUES) and \
           len({frozenset(event_ids(stack, v)) for v in (*VENUES, "central")}) == 1:
            break
        time.sleep(0.1)
    sync(*workers.values(), rounds=2)                            # let reconciliation see the final state
    catch_up_seconds = time.monotonic() - reconnected
    print(f"\nsustained cut: {reconnected - cut_started:.1f}s offline, caught up in {catch_up_seconds:.2f}s")

    # --- counts match everywhere (hand-computed: 12 registrations + 18 Stadium + 2 Hall) --------------------------
    for v in (*VENUES, "central"):
        assert stack.scalar(v, "SELECT count(*) FROM activity_events") == 32, v
        assert stack.scalar(v, "SELECT count(*) FROM (SELECT student_id, activity, kind, completion_cycle FROM activity_events "
                               "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) d") == 0, f"{v}: a duplicate completion"
        for owner in VENUES:                                     # every venue's own numbering has no hole
            seqs = [r["venue_seq"] for r in stack.q(v, "SELECT venue_seq FROM activity_events WHERE venue_id = :o ORDER BY 1", o=owner)]
            assert seqs == list(range(1, len(seqs) + 1)), (v, owner, seqs)
    assert len({frozenset(event_ids(stack, v)) for v in (*VENUES, "central")}) == 1     # the very same events
    assert len({tuple(status_rows(stack.engine(v))) for v in (*VENUES, "central")}) == 1  # and the same derived status
    for v in VENUES:
        assert stack.scalar(v, "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0, v

    # --- only the two planned PROVISIONAL exceptions ever existed, and the system closed both -----------------------
    flagged = {(r["venue_id"], r["activity"], r["student_id"]) for v in (*VENUES, "central") for r in stack.events(v, "'PROVISIONAL' = ANY(flags)")}
    assert flagged == {("stadium", "THOBE_ALLOCATION", str(stack.student_id(9))), ("hall", "THOBE_RETURN", str(stack.student_id(1)))}
    for v in (*VENUES, "central"):
        rows = stack.q(v, "SELECT type, status, student_id::text AS student FROM exceptions ORDER BY id")
        assert not [r for r in rows if r["status"] == "OPEN"], (v, rows)
        assert not [r for r in rows if r["type"] in ("CONFLICT", "SEQ_GAP", "SYNC_REJECTED")], (v, rows)
    assert stack.q("stadium", "SELECT type, status, student_id::text AS student FROM exceptions") == \
        [{"type": "PROVISIONAL_UNCONFIRMED", "status": "RESOLVED", "student": str(stack.student_id(9))}]
    assert stack.q("hall", "SELECT type, status, student_id::text AS student FROM exceptions") == \
        [{"type": "PROVISIONAL_UNCONFIRMED", "status": "RESOLVED", "student": str(stack.student_id(1))}]
    assert stack.admin("stadium").get("/admin/api/sync").json()["state"] == "ONLINE"


# =============================================================================================================
# 2. KILLING THE APPLICATION PROCESS
# =============================================================================================================
class AppProcess:
    """The real application as a real OS process (uvicorn). kill() is TerminateProcess: no `finally`, no shutdown hook."""

    def __init__(self, stack, tmp_path, port):
        self.stack, self.port, self.tmp_path, self.proc, self.starts = stack, port, tmp_path, None, 0
        self.env = {**os.environ, "MODE": "venue", "VENUE_ID": "stadium", "DATABASE_URL": stack.urls["stadium"],
                    "CENTRAL_URL": f"http://127.0.0.1:{stack.port}", "VENUE_API_KEY": stack.keys["stadium"], "SYNC_REQUIRE_TLS": "false",
                    "SYNC_INTERVAL_SECONDS": "0.1", "SYNC_BATCH_SIZE": "4", "SYNC_BACKOFF_BASE_SECONDS": "0.1",
                    "SYNC_BACKOFF_MAX_SECONDS": "0.5", "LOG_DIR": str(tmp_path / "logs"), "PYTHONUNBUFFERED": "1"}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.starts += 1
        log = open(self.tmp_path / f"app-{self.starts}.log", "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(self.port),
                                      "--log-level", "warning"], cwd=REPO_ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            assert self.proc.poll() is None, (self.tmp_path / f"app-{self.starts}.log").read_text(errors="replace")[-2000:]
            try:
                if httpx.get(f"{self.url}/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("the application did not start")

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(15)

    def restart(self):
        self.kill()
        return self.start()


def make_station(stack, activity, station_id):
    with stack.engine("stadium").begin() as c:
        users_svc.create_user(c, username=f"op-{station_id.lower()}", password=PASSWORD, role=activity)
        stations_svc.create_station(c, venue_id="stadium", station_id=station_id, activity=activity)
        return stations_svc.bind_station(c, station_id, actor_id=stack.admin_ids["stadium"])


def sign_in(app, station_id, device):
    r = httpx.post(f"{app.url}/api/login", json={"username": f"op-{station_id.lower()}", "password": PASSWORD},
                   cookies={"station_device": device}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def check_atomicity(stack, expected_events):
    """For every event the application authored (seeded rows have station SEED-1 and no outbox/audit row by design):
    exactly one event, one outbox row, one audit row and one scan-log row, or none of them."""
    q = stack.q
    events = q("stadium", "SELECT event_id::text AS id FROM activity_events WHERE venue_id = 'stadium' AND station_id IS DISTINCT FROM 'SEED-1'")
    assert len(events) == expected_events
    ids = {e["id"] for e in events}
    outbox = q("stadium", "SELECT event_id::text AS id FROM outbox")
    audit = q("stadium", "SELECT event_id::text AS id FROM audit_log WHERE action = 'ACTIVITY_CONFIRMED'")
    scans = q("stadium", "SELECT event_id::text AS id FROM scan_log WHERE event_id IS NOT NULL")
    for name, rows in (("outbox", outbox), ("audit", audit), ("scan_log", scans)):
        got = [r["id"] for r in rows]
        assert sorted(got) == sorted(ids), f"{name}: {len(got)} rows for {len(ids)} events (orphan or missing row)"


class TestKillingTheProcess:
    def test_confirms_and_the_sync_drain_survive_repeated_hard_kills(self, stack, tmp_path):
        stack.reset()
        stack.stop_central()                                       # phase A: central is down, so a backlog builds while we kill
        n = 60
        for i in range(1, n + 1):
            raw_event(stack, "stadium", i, "REGISTRATION")         # College's record, already replicated here
        devices = {s: make_station(stack, a, s) for a, s in (("THOBE_ALLOCATION", "THO-01"), ("SEATING", "SEA-01"), ("QUEUE", "QUE-01"))}
        app = AppProcess(stack, tmp_path, free_port()).start()
        tokens = {s: sign_in(app, s, d) for s, d in devices.items()}

        acknowledged, errors, failures, lock = {}, [], [], threading.Lock()
        steps = [("THOBE_ALLOCATION", "THO-01"), ("SEATING", "SEA-01"), ("QUEUE", "QUE-01")]
        deadline = time.time() + 240

        def one_student(i):
            for activity, station in steps:
                while True:
                    if time.time() > deadline:
                        failures.append(f"student {i} {activity}: timed out")
                        return
                    try:
                        r = httpx.post(f"{app.url}/confirm", json={"token": stack.token(i), "station_id": station},
                                       headers={"Authorization": f"Bearer {tokens[station]}"}, timeout=10)
                    except httpx.HTTPError as exc:                 # the process was killed under this request: an operator just presses again
                        with lock:
                            errors.append(type(exc).__name__)
                        time.sleep(0.15)
                        continue
                    if r.status_code == 200 and r.json()["result"] == "CONFIRMED":
                        with lock:
                            acknowledged[(i, activity)] = r.json()["event"]["event_id"]
                        break
                    if r.status_code == 200 and r.json()["result"] == "DUPLICATE":
                        break                                       # it had committed just before the kill: already there
                    if r.status_code in (500, 502, 503, 401):       # "One moment": retry. (401: a session lost with the kill)
                        time.sleep(0.15)
                        continue
                    failures.append(f"student {i} {activity}: {r.status_code} {r.text[:200]}")
                    return

        def killer(rounds, pause):
            for _ in range(rounds):
                time.sleep(random.uniform(*pause))
                app.restart()

        random.seed(2026)
        workers = [threading.Thread(target=lambda chunk=chunk: [one_student(i) for i in chunk]) for chunk in
                   (list(range(k, n + 1, 8)) for k in range(1, 9))]
        killer_thread = threading.Thread(target=killer, args=(4, (0.5, 1.2)))
        for t in workers:
            t.start()
        killer_thread.start()
        for t in workers:
            t.join(300)
        killer_thread.join(300)
        assert not failures, failures[:5]
        assert errors, "no request was ever interrupted: the kills never landed on live traffic, so this proves nothing"

        # every action an operator was told "Done" for is durably there; nothing is doubled; nothing is half-written
        for (i, activity), event_id in acknowledged.items():
            assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE event_id = CAST(:e AS uuid)", e=event_id) == 1, (i, activity)
        assert len(stack.events("stadium", "venue_id = 'stadium' AND kind = 'COMPLETE'")) == 3 * n          # each of 60 students x 3 steps, once
        assert stack.scalar("stadium", "SELECT count(*) FROM (SELECT student_id, activity FROM activity_events WHERE kind = 'COMPLETE' "
                                       "GROUP BY 1, 2 HAVING count(*) > 1) d") == 0
        check_atomicity(stack, 3 * n)
        assert [r["queue_position"] for r in stack.q("stadium", "SELECT queue_position FROM queue ORDER BY 1")] == list(range(1, n + 1))
        assert stack.scalar("stadium", "SELECT count(*) FROM queue q JOIN activity_events e ON e.student_id = q.student_id AND e.activity = 'QUEUE' "
                                       "AND e.kind = 'COMPLETE' WHERE (e.details->>'queue_position')::bigint = q.queue_position") == n
        seqs = [r["venue_seq"] for r in stack.q("stadium", "SELECT venue_seq FROM activity_events WHERE venue_id = 'stadium' ORDER BY 1")]
        assert seqs == list(range(1, len(seqs) + 1))                                 # no burned or skipped numbers

        # phase B: central comes back; the app drains its 180-event backlog 4 at a time and is killed mid-drain, three times
        assert stack.scalar("central", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 0
        stack.start_central()
        for _ in range(3):
            time.sleep(0.8)
            app.restart()
        drained = time.time() + 90
        while time.time() < drained and stack.scalar("stadium", "SELECT count(*) FROM outbox WHERE sent_at IS NULL") > 0:
            time.sleep(0.3)
        app.kill()
        assert stack.scalar("stadium", "SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0, "the backlog never drained"
        assert event_ids(stack, "central") == {r["event_id"] for r in stack.events("stadium", "venue_id = 'stadium'")}
        assert stack.scalar("central", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 3 * n   # exactly once each
        assert not stack.q("central", "SELECT 1 FROM exceptions WHERE type IN ('CONFLICT', 'SEQ_GAP')")
        print(f"\nkill test: {app.starts} process starts, {len(errors)} interrupted requests, {len(acknowledged)} acknowledged confirms")

    def test_a_process_killed_inside_an_open_confirm_transaction_leaves_nothing_and_burns_no_number(self, stack, tmp_path):
        stack.reset()
        stack.stop_central()
        seat_ready(stack, 1)
        seat_ready(stack, 2)
        device = make_station(stack, "QUEUE", "QUE-01")
        app = AppProcess(stack, tmp_path, free_port()).start()
        token = sign_in(app, "QUE-01", device)
        counters_before = stack.q("stadium", "SELECT name, value FROM counters ORDER BY name")

        # Hold the Stadium's event-numbering row. The Queue confirm inserts its queue row (taking a queue position)
        # and then blocks on the numbering row: the transaction is OPEN with half its work done.
        holder = stack.engine("stadium").connect()
        holder_txn = holder.begin()
        holder.execute(text("SELECT value FROM counters WHERE name = 'venue_seq:stadium' FOR UPDATE"))
        result = {}

        def confirm():
            try:
                result["r"] = httpx.post(f"{app.url}/confirm", json={"token": stack.token(1), "station_id": "QUE-01"},
                                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
            except httpx.HTTPError as exc:
                result["error"] = type(exc).__name__

        t = threading.Thread(target=confirm)
        t.start()
        deadline = time.time() + 20
        while time.time() < deadline:                              # wait until the app's transaction is stuck INSIDE the confirm
            stuck = stack.scalar("stadium", "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND "
                                            "wait_event_type = 'Lock' AND query ILIKE '%activity_events%'")
            if stuck:
                break
            time.sleep(0.05)
        assert stuck, "the confirm never reached the point of waiting on the numbering row"

        app.kill()                                                  # hard kill, transaction open
        t.join(20)
        assert "error" in result or result["r"].status_code != 200  # the operator was never told "Done"
        holder_txn.rollback()
        holder.close()
        time.sleep(1.0)                                             # let PostgreSQL notice the dead connection

        assert stack.scalar("stadium", "SELECT count(*) FROM queue") == 0
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE student_id = :s AND activity = 'QUEUE'", s=stack.student_id(1)) == 0
        assert stack.scalar("stadium", "SELECT count(*) FROM outbox") == 0
        assert stack.scalar("stadium", "SELECT count(*) FROM audit_log WHERE action = 'ACTIVITY_CONFIRMED' AND activity = 'QUEUE'") == 0
        assert stack.scalar("stadium", "SELECT count(*) FROM scan_log WHERE activity = 'QUEUE' AND event_id IS NOT NULL") == 0
        assert stack.q("stadium", "SELECT name, value FROM counters ORDER BY name") == counters_before   # no queue number was used up

        app.start()                                                 # the operator simply presses confirm again
        token = sign_in(app, "QUE-01", device)
        try:
            r = httpx.post(f"{app.url}/confirm", json={"token": stack.token(1), "station_id": "QUE-01"},
                           headers={"Authorization": f"Bearer {token}"}, timeout=20)
            assert r.status_code == 200 and r.json()["result"] == "CONFIRMED"
            assert stack.q("stadium", "SELECT queue_position FROM queue") == [{"queue_position": 1}]     # position 1, not 2
            check_atomicity(stack, 1)                                                                     # the one Queue event, whole
        finally:
            app.kill()

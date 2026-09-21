"""Phase 14: the sync engine, against the REAL topology (tests/sync_support.py): four separate databases, a real HTTP
central on a real port, real station clients authoring real events.

The four guarantees asked for, and where they are proven:

  1. The same outbox batch sent three times gives exactly one row per event_id at central
        -> TestIdempotency::test_the_same_batch_sent_three_times_leaves_exactly_one_row_per_event  (real repeated requests)
  2. Central unreachable while a venue keeps accepting scans; on reconnect every event arrives exactly once
        -> TestOutage::test_central_unreachable_then_reconnect_delivers_every_event_exactly_once
           (the port is genuinely closed, so the venue's requests get a real "connection refused")
  3. A foreign-venue write POSTed to a venue's operator API is rejected; the only way in is sync, as read-only history
        -> TestForeignWrites
  4. Derived student_status is identical whatever order events arrive in (deliberately scrambled batches)
        -> TestArrivalOrder
"""
import json
import random
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.security.ownership import ACTIVITIES, ACTIVITY_OWNER
from backend.sync import ingest, status as sync_status
from backend.sync import worker as worker_module
from backend.sync.client import SyncAuthError, SyncClient, SyncConfigError, SyncUnavailable
from backend.sync.ingest import ingest_events
from backend.sync.keys import hash_key, issue_key
from backend.sync.worker import SyncWorker, WorkerThread
from tests.sync_support import ALL, VENUES, RealServer, Stack, status_rows
from tests.test_auth import RESTRICT_VIOLATION


@pytest.fixture(scope="module")
def stack():
    s = Stack("sync", students=40)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_slate(stack):
    """Every test starts from four clean databases and a stopped central."""
    stack.reset()
    yield
    stack.stop_central()


def register(stack, client, ids):
    for i in ids:
        assert stack.confirm(client, "REG-01", i)["result"] == "CONFIRMED", i


def outbox(stack, venue):
    return stack.q(venue, "SELECT id, event_id::text AS event_id, sent_at, rejected_at, attempts, last_error FROM outbox ORDER BY id")


def unsent(stack, venue):
    return stack.scalar(venue, "SELECT count(*) FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL")


def central_client(stack, venue, **kw):
    return SyncClient(stack.server.url, stack.keys[venue], require_tls=False, **kw)


def author(stack, venue, i, activity, *, kind="COMPLETE", cycle=1, corrects=None, flags=(), reason=None, outbox=False):
    """An event written straight into `venue`'s own database (through the real Phase 2 triggers): the way to build a
    precise authored history. `outbox=True` also queues it for sync, as the station engine does. Returns its event_id."""
    details = {"reason": reason or "test"} if kind != "COMPLETE" else {}
    flags = list(flags) or (["CORRECTED"] if kind in ("WAIVER", "REVERSAL") else [])
    with stack.engine(venue).begin() as c:
        event_id = str(c.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id, flags, details, completion_cycle, "
                 "corrects_event_id) VALUES (:s, :a, :k, :v, :st, gen_random_uuid(), CAST(:f AS text[]), CAST(:d AS jsonb), :c, :x) RETURNING event_id"),
            {"s": stack.student_id(i), "a": activity, "k": kind, "v": venue, "st": None if kind in ("WAIVER", "REVERSAL") else "SEED",
             "f": flags, "d": json.dumps(details), "c": cycle, "x": corrects}).scalar_one())
        if outbox:
            c.execute(text("INSERT INTO outbox (event_id, payload) SELECT event_id, to_jsonb(e) FROM activity_events e WHERE event_id = :e"), {"e": event_id})
        return event_id


def payloads(stack, venue):
    return stack.q(venue, "SELECT to_jsonb(e) AS p FROM activity_events e WHERE venue_id = :v ORDER BY venue_seq", v=venue)


def build_history(stack):
    """A history that exercises every ordering hazard: reversals, a second and third cycle, a waiver, a skip, and
    journeys that cross all three venues. Returns {venue: [payload, ...]} in authored order."""
    for i in (1, 2, 3):                                     # full journeys through all seven activities
        for a in ACTIVITIES:
            author(stack, ACTIVITY_OWNER[a], i, a)
    reg = author(stack, "college", 4, "REGISTRATION")
    alloc = author(stack, "stadium", 4, "THOBE_ALLOCATION")
    author(stack, "stadium", 4, "THOBE_ALLOCATION", kind="REVERSAL", corrects=alloc, reason="wrong student")
    author(stack, "stadium", 4, "THOBE_ALLOCATION", cycle=2)                       # cycle 2 depends on the reversal
    author(stack, "stadium", 4, "SEATING")
    for i in (5,):                                          # a lost thobe: waiver instead of a return
        for a in ACTIVITIES[:5]:
            author(stack, ACTIVITY_OWNER[a], i, a)
        author(stack, "hall", i, "THOBE_RETURN", kind="WAIVER", reason="lost")
        author(stack, "hall", i, "LUNCH")
    c1 = author(stack, "college", 6, "REGISTRATION")                              # three cycles, the last one active
    author(stack, "college", 6, "REGISTRATION", kind="REVERSAL", corrects=c1, reason="a")
    c2 = author(stack, "college", 6, "REGISTRATION", cycle=2)
    author(stack, "college", 6, "REGISTRATION", kind="REVERSAL", cycle=2, corrects=c2, reason="b")
    author(stack, "college", 6, "REGISTRATION", cycle=3)
    for a in ACTIVITIES[:4]:                                # a skip, then the real completion
        author(stack, ACTIVITY_OWNER[a], 7, a)
    author(stack, "stadium", 7, "STAGE", kind="SKIP", reason="not ready")
    author(stack, "stadium", 7, "STAGE")
    author(stack, "college", 8, "REGISTRATION")
    return {v: [r["p"] for r in payloads(stack, v)] for v in VENUES}


# =========================================================================== 1. IDEMPOTENCY
class TestIdempotency:
    def test_the_same_batch_sent_three_times_leaves_exactly_one_row_per_event(self, stack):
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        register(stack, registration, range(1, 13))
        batch = [r["payload"] for r in stack.q("college", "SELECT payload FROM outbox ORDER BY id")]
        assert len(batch) == 12
        client = central_client(stack, "college")

        answers = [client.push(batch, pending_after=0, last_seq=12, drain_total=0, drain_done=0) for _ in range(3)]  # 3 real HTTP requests

        statuses = [[r["status"] for r in a["results"]] for a in answers]
        assert statuses[0] == ["ACCEPTED"] * 12
        assert statuses[1] == ["DUPLICATE"] * 12 and statuses[2] == ["DUPLICATE"] * 12
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 12
        assert stack.scalar("central", "SELECT count(DISTINCT event_id) FROM activity_events") == 12       # one row per event_id
        assert stack.scalar("central", "SELECT count(*) FROM sync_log") == 12                                # numbered once, too
        assert [r["central_seq"] for r in stack.q("central", "SELECT central_seq FROM sync_log ORDER BY central_seq")] == list(range(1, 13))
        assert stack.events("central") == stack.events("college")   # what central holds is exactly what the venue wrote

    def test_a_single_batch_containing_the_same_event_twice_also_stores_it_once(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1])
        one = stack.q("college", "SELECT payload FROM outbox")[0]["payload"]
        answer = central_client(stack, "college").push([one, one, one], pending_after=0, last_seq=1, drain_total=0, drain_done=0)
        assert [r["status"] for r in answer["results"]] == ["ACCEPTED", "DUPLICATE", "DUPLICATE"]
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 1

    def test_the_worker_marks_an_event_sent_only_after_central_confirms_it(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1, 2, 3])
        assert unsent(stack, "college") == 3
        stack.stop_central()
        worker = stack.worker("college")
        assert not worker.cycle().ok
        assert unsent(stack, "college") == 3 and all(r["sent_at"] is None for r in outbox(stack, "college"))   # nothing marked
        stack.start_central()
        report = worker.cycle()
        assert report.ok and report.pushed == 3
        assert unsent(stack, "college") == 0 and all(r["sent_at"] is not None for r in outbox(stack, "college"))


# =========================================================================== 2. OUTAGE
class TestOutage:
    def test_central_unreachable_then_reconnect_delivers_every_event_exactly_once(self, stack):
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        worker = stack.worker("college")
        register(stack, registration, range(1, 6))
        assert worker.cycle().ok and stack.scalar("central", "SELECT count(*) FROM activity_events") == 5

        stack.stop_central()                                     # CENTRAL GOES DOWN: the port is really closed
        with pytest.raises(SyncUnavailable):
            SyncClient(f"http://127.0.0.1:{stack.port}", stack.keys["college"], require_tls=False).pull(0, 1)   # a real refused connection

        failed = []
        for i in range(6, 31):                                   # the venue keeps accepting scans, normally
            assert stack.confirm(registration, "REG-01", i)["result"] == "CONFIRMED"
            if i % 8 == 0:
                failed.append(worker.cycle())                    # ... and keeps trying to sync, in vain
        assert all(not r.ok for r in failed) and worker.failures == len(failed)
        assert stack.scalar("college", "SELECT count(*) FROM activity_events") == 30
        assert unsent(stack, "college") == 25                    # everything since the outage began is waiting
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 5

        stack.start_central()                                    # CENTRAL COMES BACK
        report = worker.cycle()
        assert report.ok and report.pushed == 25 and worker.failures == 0
        assert stack.events("central") == stack.events("college")                                        # every event, no more, no fewer
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == stack.scalar("central", "SELECT count(DISTINCT event_id) FROM activity_events") == 30
        assert unsent(stack, "college") == 0
        assert [r["venue_seq"] for r in stack.q("central", "SELECT venue_seq FROM activity_events ORDER BY venue_seq")] == list(range(1, 31))
        assert worker.cycle().pushed == 0                        # and a further cycle sends nothing new

    def test_a_crash_after_central_commits_loses_nothing_and_duplicates_nothing(self, stack, monkeypatch):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 11))
        worker = stack.worker("college")

        def process_dies(*a, **k):
            raise RuntimeError("the venue process was killed after central answered")
        monkeypatch.setattr(worker_module, "_mark", process_dies)
        assert not worker.cycle().ok
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 10   # central already committed them ...
        assert unsent(stack, "college") == 10                                          # ... but the venue never got to mark them
        monkeypatch.undo()

        report = worker.cycle()                                                        # restart: everything is sent again
        assert report.ok and report.pushed == 10
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == stack.scalar("central", "SELECT count(DISTINCT event_id) FROM activity_events") == 10
        assert stack.scalar("central", "SELECT count(*) FROM sync_log") == 10
        assert unsent(stack, "college") == 0

    def test_retry_backs_off_exponentially_up_to_a_cap_and_resets_on_success(self, stack):
        worker = stack.worker("college", sync_backoff_base_seconds=1.0, sync_backoff_max_seconds=8.0)
        worker.rng = lambda: 1.0                                # no jitter, so the sequence is exact
        assert not worker.cycle().ok and worker.failures == 1   # one REAL failed cycle (central is stopped) moves the counter
        delays = []
        for n in range(1, 7):                                   # then the delay after n failures in a row
            worker.failures = n
            delays.append(worker.backoff_delay())
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
        worker.rng = lambda: 0.0
        assert worker.backoff_delay() == 4.0                    # jitter lowers a delay to at least half
        stack.start_central()
        assert worker.cycle().ok and worker.failures == 0       # a success clears it

    def test_the_background_worker_syncs_by_itself_and_stops_cleanly(self, stack):
        import time
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        thread = WorkerThread(stack.worker("college")).start()
        try:
            register(stack, registration, [1, 2, 3])
            deadline = time.time() + 15
            while stack.scalar("central", "SELECT count(*) FROM activity_events") < 3 and time.time() < deadline:
                time.sleep(0.05)
            assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 3
            register(stack, registration, [4, 5])                # later scans are picked up without anyone asking
            while stack.scalar("central", "SELECT count(*) FROM activity_events") < 5 and time.time() < deadline:
                time.sleep(0.05)
            assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 5
        finally:
            thread.stop()
        assert not thread.thread.is_alive()

    def test_an_event_for_a_student_central_does_not_know_yet_is_retried_then_delivered(self, stack):
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        with stack.engine("college").begin() as c:               # a student loaded at the venue but not yet at central
            c.execute(text("INSERT INTO students (id, prn, name, programme, school, sequence_no) VALUES "
                           "('99999999-0000-0000-0000-000000000001', 'NEW1', 'Late Arrival', 'B.Tech', 'School of Law', 9001)"))
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES ('99999999-0000-0000-0000-000000000001', 'token-late')"))
        assert registration.post("/confirm", json={"token": "token-late", "station_id": "REG-01"}).json()["result"] == "CONFIRMED"
        worker = stack.worker("college")
        report = worker.cycle()
        assert report.ok and report.pushed == 0                  # not acknowledged: central said RETRY
        row = outbox(stack, "college")[0]
        assert row["sent_at"] is None and row["rejected_at"] is None and row["attempts"] == 1
        with stack.engine("central").begin() as c:               # the master data arrives at central
            c.execute(text("INSERT INTO students (id, prn, name, programme, school, sequence_no) VALUES "
                           "('99999999-0000-0000-0000-000000000001', 'NEW1', 'Late Arrival', 'B.Tech', 'School of Law', 9001)"))
        assert worker.cycle().pushed == 1
        assert outbox(stack, "college")[0]["sent_at"] is not None
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 1

    def test_a_transient_refusal_that_never_clears_stops_being_retried_and_is_raised_for_the_admin(self, stack):
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        with stack.engine("college").begin() as c:
            c.execute(text("INSERT INTO students (id, prn, name, programme, school, sequence_no) VALUES "
                           "('99999999-0000-0000-0000-000000000002', 'NEW2', 'Nobody At Central', 'B.Tech', 'School of Law', 9002)"))
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES ('99999999-0000-0000-0000-000000000002', 'token-nobody')"))
        registration.post("/confirm", json={"token": "token-nobody", "station_id": "REG-01"})
        worker = stack.worker("college", sync_max_attempts=3)
        for _ in range(3):
            worker.cycle()
        row = outbox(stack, "college")[0]
        assert row["rejected_at"] is not None and row["sent_at"] is None                 # never falsely marked sent
        assert stack.scalar("college", "SELECT count(*) FROM exceptions WHERE type = 'SYNC_REJECTED' AND status = 'OPEN'") == 1
        assert worker.cycle().pushed == 0 and outbox(stack, "college")[0]["attempts"] == 3   # and no longer retried


# =========================================================================== 3. FOREIGN WRITES
class TestForeignWrites:
    def test_a_foreign_venue_write_posted_to_the_operator_api_is_rejected(self, stack):
        """The Stadium is asked to record a Registration (a College activity) through its own operator API."""
        from backend import stations as stations_svc, users as users_svc
        from tests.test_auth import PASSWORD, api_login, new_client
        with stack.engine("stadium").begin() as c:               # a College station, planted in the Stadium's database
            users_svc.create_user(c, username="op-foreign", password=PASSWORD, role="REGISTRATION")
            stations_svc.create_station(c, venue_id="college", station_id="REG-X", activity="REGISTRATION")
            device = stations_svc.bind_station(c, "REG-X", actor_id=stack.admin_ids["stadium"])
        before = (stack.scalar("stadium", "SELECT count(*) FROM activity_events"), stack.scalar("stadium", "SELECT count(*) FROM outbox"))
        # layer 1: the operator cannot even sign in at a server that does not own that station's activity
        assert api_login(new_client(stack.app("stadium"), device), "op-foreign").status_code == 403
        # layer 2: even an Admin session is refused when it tries to record it
        admin = stack.admin("stadium")
        for path in ("/confirm", "/scan"):
            r = admin.post(path, json={"token": stack.token(1), "station_id": "REG-X"})
            assert r.status_code == 403, (path, r.text)
            assert "College" in json.dumps(r.json()) and "not here" in json.dumps(r.json())
        assert admin.post("/confirm", json={"student_id": str(stack.student_id(1)), "station_id": "REG-X"}).status_code == 403   # manual PRN path too
        assert (stack.scalar("stadium", "SELECT count(*) FROM activity_events"), stack.scalar("stadium", "SELECT count(*) FROM outbox")) == before
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE activity = 'REGISTRATION'") == 0

    def test_a_venue_server_has_no_sync_endpoint_at_all_so_nothing_can_be_pushed_at_it(self, stack):
        one = {"event_id": "00000000-0000-0000-0000-000000000001"}
        for venue in VENUES:
            client = TestClient(stack.app(venue))
            for method, path in (("post", "/sync/push"), ("get", "/sync/pull")):
                r = getattr(client, method)(path, headers={"Authorization": f"Bearer {stack.keys['college']}"}, **({"json": {"events": [one]}} if method == "post" else {}))
                assert r.status_code == 404, (venue, path)
        assert TestClient(stack.app("central")).get("/sync/pull", headers={"Authorization": f"Bearer {stack.keys['college']}"}).status_code == 200

    def test_central_refuses_an_event_the_sending_venue_does_not_own(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1])
        registration_event = stack.q("college", "SELECT payload FROM outbox")[0]["payload"]
        hall = central_client(stack, "hall")
        answer = hall.push([registration_event], pending_after=0, last_seq=None, drain_total=0, drain_done=0)
        assert answer["results"][0]["status"] == "REJECTED" and "hall" in answer["results"][0]["reason"]
        forged = {**registration_event, "activity": "LUNCH"}     # claims to be a College event about a Hall activity
        assert central_client(stack, "college").push([forged], pending_after=0, last_seq=None, drain_total=0, drain_done=0)["results"][0]["status"] == "REJECTED"
        also = {**registration_event, "venue_id": "hall"}        # a College key sending a "Hall" event
        assert central_client(stack, "college").push([also], pending_after=0, last_seq=None, drain_total=0, drain_done=0)["results"][0]["status"] == "REJECTED"
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 0

    def test_the_only_way_in_is_sync_and_what_arrives_is_read_only_history(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1, 2])
        assert stack.worker("college").cycle().ok
        stadium = stack.worker("stadium")
        assert stadium.cycle().ok
        foreign = stack.events("stadium", "venue_id = 'college'")
        assert len(foreign) == 2 and stack.events("stadium") == foreign                    # the College's events, stored at the Stadium
        assert stack.scalar("stadium", "SELECT count(*) FROM outbox") == 0                 # never re-sent by the Stadium
        original = foreign[0]["event_id"]
        r = stack.admin("stadium").post("/admin/api/corrections/reverse", json={"event_id": original, "reason": "trying to edit history"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "APPLY_AT_OWNER"      # it cannot be changed here
        assert stack.events("stadium") == foreign
        # and the Stadium still cannot RECORD a registration: there is no such station, and a College one is refused (test above)
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 0


# =========================================================================== KEYS AND TLS
class TestKeysAndTls:
    def test_a_bad_missing_or_revoked_key_is_refused(self, stack):
        stack.start_central()
        assert central_client(stack, "college").pull(0, 5)["events"] == []
        with pytest.raises(SyncAuthError):
            SyncClient(stack.server.url, "vk_not_a_real_key", require_tls=False).pull(0, 5)
        request = urllib.request.Request(stack.server.url + "/sync/pull")               # no key at all
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 401
        old = stack.keys["college"]
        with stack.engine("central").begin() as c:                                        # rotation: issuing a new key revokes the old
            new = issue_key(c, "college", "rotated")
        with pytest.raises(SyncAuthError):
            SyncClient(stack.server.url, old, require_tls=False).pull(0, 5)
        assert SyncClient(stack.server.url, new, require_tls=False).pull(0, 5)["events"] == []
        with stack.engine("central").begin() as c:
            c.execute(text("UPDATE venue_api_keys SET revoked_at = now() WHERE venue_id = 'college' AND revoked_at IS NULL"))
        with pytest.raises(SyncAuthError):
            SyncClient(stack.server.url, new, require_tls=False).pull(0, 5)

    def test_keys_are_stored_only_as_a_hash_and_cannot_be_altered(self, stack):
        key = stack.keys["stadium"]
        rows = stack.q("central", "SELECT venue_id, key_hash FROM venue_api_keys WHERE revoked_at IS NULL")
        assert {r["venue_id"] for r in rows} == set(VENUES)
        assert [r["key_hash"] for r in rows if r["venue_id"] == "stadium"] == [hash_key(key)] and len(hash_key(key)) == 64
        assert stack.scalar("central", "SELECT count(*) FROM venue_api_keys WHERE key_hash = :k OR note = :k", k=key) == 0   # the key itself is nowhere
        assert key.startswith("vk_") and len(key) > 40
        for sql in ("UPDATE venue_api_keys SET key_hash = 'x'", "DELETE FROM venue_api_keys"):
            with stack.engine("central").connect() as c:
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql))
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION

    def test_a_venue_refuses_to_send_its_key_over_plain_http(self, stack):
        with pytest.raises(SyncConfigError) as exc:
            SyncClient("http://central.example.edu", "vk_x")                              # TLS is required by default
        assert "https" in str(exc.value)
        SyncClient("http://central.example.edu", "vk_x", require_tls=False)              # only a closed test network may opt out
        assert stack.settings("college", sync_require_tls=True).sync_require_tls is True
        with pytest.raises(SyncConfigError):
            SyncWorker(stack.engine("college"), stack.settings("college", sync_require_tls=True))   # the http test URL is refused
        with pytest.raises(SyncConfigError):
            SyncClient(None, None)

    @pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is needed to make a throwaway certificate")
    def test_sync_works_over_real_tls_and_a_wrong_certificate_is_refused(self, stack, tmp_path):
        cert, key = tmp_path / "central.pem", tmp_path / "central.key"
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
                               "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
        server = RealServer(stack.app("central"), stack.port + 1, certfile=str(cert), keyfile=str(key)).start()
        try:
            register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1, 2])
            trusted = SyncWorker(stack.engine("college"), stack.settings("college", central_url=server.url, sync_require_tls=True,
                                                                        central_ca_file=str(cert)))
            report = trusted.cycle()
            assert report.ok and report.pushed == 2                                        # https, certificate verified
            assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 2
            untrusting = SyncClient(server.url, stack.keys["college"], require_tls=True)   # default trust store: our cert is unknown
            with pytest.raises(SyncUnavailable):
                untrusting.pull(0, 1)
        finally:
            server.stop()


# =========================================================================== 4. ARRIVAL ORDER
class TestArrivalOrder:
    def _ingest_in_batches(self, engine, events, rng, *, max_batch=6):
        parked = 0
        pending = list(events)
        while pending:
            size = rng.randint(1, max_batch)
            batch, pending = pending[:size], pending[size:]
            with engine.begin() as c:
                results = ingest_events(c, batch, log_arrivals=True, source="test")
            parked += sum(1 for r in results if r.status == "PARKED")
        return parked

    def test_derived_status_is_identical_however_the_events_arrive(self, stack):
        history = build_history(stack)
        everything = [p for v in VENUES for p in history[v]]
        assert len(everything) > 40
        canonical = stack.scratch("canonical")
        with canonical.begin() as c:                                    # in the order they were authored
            assert all(r.status == "ACCEPTED" for r in ingest_events(c, everything, log_arrivals=True))
        expected_status = status_rows(canonical)
        expected_events = sorted(p["event_id"] for p in everything)
        assert len({s[2] for s in expected_status}) > 4                # the history really spans many different statuses

        parked_in_total = 0
        for seed in range(6):                                           # six deliberately scrambled deliveries
            engine = stack.scratch(f"scrambled{seed}")
            rng = random.Random(seed)
            scrambled = list(everything)
            rng.shuffle(scrambled) if seed else scrambled.reverse()    # seed 0: exactly REVERSED (every reversal before its original)
            parked_in_total += self._ingest_in_batches(engine, scrambled, rng)
            assert status_rows(engine) == expected_status, f"seed {seed}"
            with engine.connect() as c:
                assert [r[0] for r in c.execute(text("SELECT event_id::text FROM activity_events ORDER BY event_id::text"))] == sorted(expected_events)
                assert c.execute(text("SELECT count(*) FROM sync_parked")).scalar_one() == 0        # nothing left waiting
                assert c.execute(text("SELECT count(*) FROM conflict_events")).scalar_one() == 0     # and nothing was mistaken for a duplicate
                assert c.execute(text("SELECT count(*) FROM sync_log")).scalar_one() == len(everything)
        assert parked_in_total > 0                                     # the scrambling really did hit dependency parking

    def test_a_scrambled_batch_through_the_real_endpoint_gives_the_same_status(self, stack):
        history = build_history(stack)
        canonical = stack.scratch("canonical2")
        with canonical.begin() as c:
            ingest_events(c, [p for v in VENUES for p in history[v]], log_arrivals=True)
        stack.start_central()
        rng = random.Random(7)
        for venue in VENUES:
            events = list(history[venue])
            events.reverse()                                            # newest first: reversals and cycle 2 BEFORE their originals
            halves = [events[: len(events) // 2], events[len(events) // 2:]]
            for part in halves:
                answer = central_client(stack, venue).push(part, pending_after=0, last_seq=None, drain_total=0, drain_done=0)
                assert all(r["status"] in ("ACCEPTED", "PARKED", "DUPLICATE") for r in answer["results"])
        assert stack.scalar("central", "SELECT count(*) FROM sync_parked") == 0
        assert status_rows(stack.engine("central")) == status_rows(canonical)

    def test_a_venue_applying_the_other_venues_events_in_a_scrambled_order_ends_the_same(self, stack):
        history = build_history(stack)
        foreign = history["college"] + history["hall"]                 # what the Stadium pulls
        reference = stack.scratch("stadium-ref")
        with reference.begin() as c:
            ingest_events(c, foreign, skip_venue="stadium")
        for seed in (1, 2, 3):
            engine = stack.scratch(f"stadium-scr{seed}")
            scrambled = list(foreign)
            random.Random(seed).shuffle(scrambled)
            with engine.begin() as c:
                results = ingest_events(c, scrambled, skip_venue="stadium")
            assert all(r.status in ("ACCEPTED", "PARKED") for r in results)
            assert status_rows(engine) == status_rows(reference)
            with engine.connect() as c:
                assert c.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0

    def test_an_event_whose_dependency_never_arrives_stays_parked_and_shows_as_a_gap(self, stack):
        stack.start_central()
        first = author(stack, "college", 1, "REGISTRATION")
        reversal = author(stack, "college", 1, "REGISTRATION", kind="REVERSAL", corrects=first, reason="x")
        only_the_reversal = payloads(stack, "college")[1]["p"]
        answer = central_client(stack, "college").push([only_the_reversal], pending_after=0, last_seq=2, drain_total=0, drain_done=0)
        assert answer["results"][0]["status"] == "PARKED"
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 0
        assert stack.scalar("central", "SELECT count(*) FROM sync_parked") == 1           # held durably, not dropped
        assert stack.scalar("central", "SELECT count(*) FROM exceptions WHERE type = 'SEQ_GAP' AND status = 'OPEN'") >= 1
        original = payloads(stack, "college")[0]["p"]                                    # the original finally arrives
        central_client(stack, "college").push([original], pending_after=0, last_seq=2, drain_total=0, drain_done=0)
        assert stack.scalar("central", "SELECT count(*) FROM sync_parked") == 0 and stack.scalar("central", "SELECT count(*) FROM activity_events") == 2
        assert stack.scalar("central", "SELECT count(*) FROM exceptions WHERE type = 'SEQ_GAP' AND status = 'OPEN'") == 0   # the gap closed itself


# =========================================================================== PULL, FRESHNESS, GAPS, STATUS
class TestPullAndFreshness:
    def test_pull_is_cursor_based_and_idempotent(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 31))
        assert stack.worker("college").cycle().ok
        stadium = stack.worker("stadium", sync_batch_size=7)                   # five pages of seven
        assert stadium.pull_once() == 30
        assert stack.events("stadium") == stack.events("college")
        cursor = stack.scalar("stadium", "SELECT cursor FROM sync_state WHERE peer = 'central'")
        assert cursor == stack.scalar("central", "SELECT max(central_seq) FROM sync_log") == 30
        assert stadium.pull_once() == 0                                       # nothing new: nothing applied
        with stack.engine("stadium").begin() as c:
            c.execute(text("UPDATE sync_state SET cursor = 0 WHERE peer = 'central'"))   # forget where we were: re-pull it all
        assert stadium.pull_once() == 0                                       # every one is a DUPLICATE
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events") == 30
        assert stack.scalar("stadium", "SELECT count(*) FROM outbox") == 0

    def test_a_venue_never_pulls_its_own_events_back(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1, 2, 3])
        assert stack.worker("college").cycle().ok and stack.worker("stadium").cycle().ok
        for i in (1, 2, 3):
            assert stack.confirm(stack.operator("stadium", "THOBE_ALLOCATION", "THO-01"), "THO-01", i)["result"] == "CONFIRMED"
        assert stack.worker("stadium").cycle().ok
        college = stack.worker("college")
        assert college.cycle().ok
        assert stack.scalar("college", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 3   # the Stadium's, from central
        assert stack.scalar("college", "SELECT count(*) FROM activity_events WHERE venue_id = 'college'") == 3   # its own, untouched
        stadium_again = stack.worker("stadium")
        assert stadium_again.pull_once() == 0 and stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE venue_id = 'stadium'") == 3

    def test_freshness_is_the_peers_own_last_report_not_our_last_pull(self, stack):
        stack.start_central()
        assert stack.worker("college").cycle().ok                             # the College reports in
        stadium = stack.worker("stadium")
        assert stadium.cycle().ok
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_is_fresh(c, "college") is True             # the College reported seconds ago
            assert sync_status.peer_is_fresh(c, "hall") is False               # the Hall has never reported to central: stale
        with stack.engine("central").begin() as c:                            # the College last reported TEN MINUTES ago ...
            c.execute(text("UPDATE sync_state SET last_success_at = now() - interval '10 minutes' WHERE peer = 'college'"))
        assert stadium.cycle().ok                                              # ... while the Stadium's own pull keeps succeeding
        with stack.engine("stadium").connect() as c:
            assert 590 < sync_status.peer_age_seconds(c, "college") < 640
            assert sync_status.peer_is_fresh(c, "college") is False            # a successful pull does NOT make an absent peer look fresh

    def test_the_window_is_configurable_and_defaults_to_two_minutes(self, stack):
        stack.start_central()
        assert stack.scalar("stadium", "SELECT freshness_window_seconds FROM settings") == 120
        stack.set_peer_freshness("stadium", "college", 200)
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_is_fresh(c, "college") is False
        admin = stack.admin("stadium")
        assert admin.put("/admin/api/sync/freshness-window", json={"seconds": 600}).status_code == 200
        with stack.engine("stadium").connect() as c:
            assert sync_status.freshness_window(c) == 600 and sync_status.peer_is_fresh(c, "college") is True
        assert admin.put("/admin/api/sync/freshness-window", json={"seconds": 5}).status_code == 400          # too small to be safe
        assert admin.put("/admin/api/sync/freshness-window", json={"seconds": 99999}).status_code == 400
        assert stack.scalar("stadium", "SELECT count(*) FROM audit_log WHERE action = 'FRESHNESS_WINDOW_CHANGED'") == 1
        with stack.engine("stadium").begin() as c:
            c.execute(text("UPDATE settings SET freshness_window_seconds = 120"))

    def test_a_partial_pull_does_not_refresh_freshness(self, stack, monkeypatch):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 7))
        assert stack.worker("college").cycle().ok
        stadium = stack.worker("stadium", sync_batch_size=2)
        monkeypatch.setattr(worker_module, "MAX_BATCHES_PER_CYCLE", 1)          # stop after ONE page of two
        assert stadium.pull_once() == 2
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_age_seconds(c, "college") is None            # not caught up: we do not know the peer is current
        monkeypatch.undo()
        assert stadium.pull_once() == 4
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_is_fresh(c, "college") is True               # caught up: now we do


class TestGapsAndStatus:
    def _eight(self, stack):
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 9))
        return [r["payload"] for r in stack.q("college", "SELECT payload FROM outbox ORDER BY id")]

    def test_a_missing_venue_seq_raises_an_exception_that_closes_itself_when_it_arrives(self, stack):
        stack.start_central()
        events = self._eight(stack)
        by_seq = {e["venue_seq"]: e for e in events}
        client = central_client(stack, "college")
        client.push([by_seq[s] for s in (1, 2, 3, 5, 6)], pending_after=0, last_seq=8, drain_total=0, drain_done=0)
        gaps = stack.q("central", "SELECT details, status FROM exceptions WHERE type = 'SEQ_GAP' ORDER BY id")
        assert [(g["details"]["first_missing"], g["details"]["last_missing"]) for g in gaps] == [(4, 4), (7, 8)]   # one inside, one at the tail
        assert all(g["status"] == "OPEN" for g in gaps)
        client.push([by_seq[s] for s in (1, 2, 3, 5, 6)], pending_after=0, last_seq=8, drain_total=0, drain_done=0)   # again: no new items
        assert stack.scalar("central", "SELECT count(*) FROM exceptions WHERE type = 'SEQ_GAP'") == 2
        client.push([by_seq[4]], pending_after=0, last_seq=8, drain_total=0, drain_done=0)
        assert [g["status"] for g in stack.q("central", "SELECT status FROM exceptions WHERE type = 'SEQ_GAP' ORDER BY id")] == ["RESOLVED", "OPEN"]
        client.push([by_seq[7], by_seq[8]], pending_after=0, last_seq=8, drain_total=0, drain_done=0)
        closed = stack.q("central", "SELECT status, resolved_by, resolution_note FROM exceptions WHERE type = 'SEQ_GAP'")
        assert all(g["status"] == "RESOLVED" and g["resolved_by"] is None for g in closed)          # closed by the system, not a person
        assert stack.scalar("central", "SELECT count(*) FROM audit_log WHERE action = 'EXCEPTION_AUTO_CLOSED'") == 2

    def test_an_event_still_in_transit_is_not_a_gap(self, stack):
        stack.start_central()
        events = self._eight(stack)
        central_client(stack, "college").push(events[:5], pending_after=3, last_seq=8, drain_total=8, drain_done=5)  # 3 still waiting at the venue
        assert stack.scalar("central", "SELECT count(*) FROM exceptions WHERE type = 'SEQ_GAP'") == 0

    def test_the_traffic_light_shows_green_amber_and_blue_at_central_and_on_the_dashboard(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 13))
        assert stack.worker("stadium").cycle().ok                                # the Stadium is in sync with nothing to send

        class GoesDownAfterOneBatch:                                             # the College's link drops part-way through
            def __init__(self, real):
                self.real, self.pushes = real, 0

            def push(self, *a, **k):
                self.pushes += 1
                if self.pushes > 1:
                    raise SyncUnavailable("the link dropped")
                return self.real.push(*a, **k)

            def pull(self, *a, **k):
                return self.real.pull(*a, **k)

        college = stack.worker("college", sync_batch_size=5)
        college.client = GoesDownAfterOneBatch(college.client)
        assert not college.cycle().ok
        assert unsent(stack, "college") == 7                                     # 5 of 12 went through

        with stack.engine("central").connect() as c:
            seen = {v["venue"]: v for v in sync_status.venues_seen_by_central(c)}
        assert (seen["stadium"]["emoji"], seen["stadium"]["label"]) == ("🟢", "ONLINE — all synced")
        assert (seen["college"]["emoji"], seen["college"]["label"]) == ("🔵", "SYNCING 5 / 12")
        assert (seen["hall"]["emoji"], seen["hall"]["label"]) == ("🟡", "OFFLINE — never connected")
        with stack.engine("college").connect() as c:                             # and the College's own view of itself
            mine = sync_status.this_server(c, stack.settings("college"))
        assert (mine["emoji"], mine["label"], mine["pending"]) == ("🔵", "SYNCING 5 / 12", 7)

        page = stack.admin("central").get("/admin/dashboard/live").text          # surfaced on the Admin dashboard (central) ...
        assert "🟢 ONLINE — all synced" in page and "🔵 SYNCING 5 / 12" in page and "🟡 OFFLINE — never connected" in page
        mine_page = stack.admin("college").get("/admin/dashboard/live").text     # ... and at the venue
        assert "🔵" in mine_page and "SYNCING 5 / 12" in mine_page
        health = stack.admin("central").get("/admin/api/dashboard").json()["health"]
        assert {v["venue"]: v["state"] for v in health["venues"]} == {"college": "SYNCING", "stadium": "ONLINE", "hall": "OFFLINE"}

        college.client = college.client.real                                      # the link comes back
        assert college.cycle().ok and unsent(stack, "college") == 0
        with stack.engine("central").connect() as c:
            assert {v["venue"]: v["emoji"] for v in sync_status.venues_seen_by_central(c)}["college"] == "🟢"

    def test_a_venue_that_has_not_been_heard_from_within_the_window_shows_amber_with_its_backlog(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 4))
        college = stack.worker("college")
        assert college.cycle().ok
        stack.stop_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(4, 9))
        assert not college.cycle().ok
        with stack.engine("college").begin() as c:                               # let the window pass (we age the timestamps, not sleep)
            c.execute(text("UPDATE sync_state SET last_push_at = now() - interval '5 minutes', last_success_at = now() - interval '5 minutes' WHERE peer = 'central'"))
        with stack.engine("college").connect() as c:
            mine = sync_status.this_server(c, stack.settings("college"))
        assert (mine["emoji"], mine["label"]) == ("🟡", "OFFLINE — LOCAL MODE · 5 waiting")
        with stack.engine("central").begin() as c:
            c.execute(text("UPDATE sync_state SET last_success_at = now() - interval '5 minutes' WHERE peer = 'college'"))
        with stack.engine("central").connect() as c:
            seen = {v["venue"]: v for v in sync_status.venues_seen_by_central(c)}
        assert seen["college"]["emoji"] == "🟡" and seen["college"]["label"].startswith("OFFLINE — LOCAL MODE")
        assert "🟡" in stack.admin("college").get("/admin/dashboard/live").text

    def test_the_epoch_changes_restart_the_venues_cursor(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), range(1, 6))
        assert stack.worker("college").cycle().ok
        stadium = stack.worker("stadium")
        assert stadium.pull_once() == 5
        with stack.engine("central").begin() as c:                               # central is rebuilt: same events, new numbering, new epoch
            c.execute(text("UPDATE sync_meta SET value = gen_random_uuid()::text WHERE key = 'epoch'"))
        with stack.engine("stadium").begin() as c:
            c.execute(text("UPDATE sync_state SET cursor = 9999 WHERE peer = 'central'"))    # a cursor beyond anything central now has
        assert stadium.pull_once() == 0                                          # everything re-pulled: all already held, none applied twice
        assert stack.scalar("stadium", "SELECT cursor FROM sync_state WHERE peer = 'central'") == 5
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events") == 5


# =========================================================================== CONCURRENCY AND THE APP'S OWN WORKER
class TestConcurrencyAndLifecycle:
    def test_concurrent_pushes_and_pulls_never_skip_or_duplicate_an_event(self, stack):
        """The classic cursor bug: two transactions commit out of order and a puller's cursor jumps past the late one.
        central_seq is numbered in COMMIT order under a lock, so this must never lose an event."""
        import threading
        stack.start_central()
        for i in range(1, 31):
            author(stack, "college", i, "REGISTRATION", outbox=True)
            author(stack, "hall", i, "THOBE_RETURN", outbox=True)
        college, hall = stack.worker("college", sync_batch_size=4), stack.worker("hall", sync_batch_size=4)
        stadium = stack.worker("stadium", sync_batch_size=3)
        done, errors = threading.Event(), []

        def push(worker):
            try:
                while unsent(stack, worker.venue):
                    assert worker.push_once()[1]
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def pull():
            try:
                while not done.is_set():
                    stadium.pull_once()                                 # pulling WHILE the others are still pushing
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        puller = threading.Thread(target=pull)
        pushers = [threading.Thread(target=push, args=(w,)) for w in (college, hall)]
        puller.start()
        [t.start() for t in pushers]
        [t.join(120) for t in pushers]
        done.set()
        puller.join(60)
        assert not errors, errors
        stadium.pull_once()                                             # one last pull once everything has landed
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 60
        assert [r["central_seq"] for r in stack.q("central", "SELECT central_seq FROM sync_log ORDER BY central_seq")] == list(range(1, 61))
        assert stack.events("stadium") == stack.events("central")       # the Stadium holds all 60, none skipped, none twice
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events") == stack.scalar("stadium", "SELECT count(DISTINCT event_id) FROM activity_events") == 60

    def test_the_app_starts_its_own_sync_worker_and_stops_it_and_a_central_or_unconfigured_app_does_not(self, stack):
        import time
        stack.start_central()
        registration = stack.operator("college", "REGISTRATION", "REG-01")
        with TestClient(stack.app("college")) as client:                # entering the client runs the app's lifespan
            thread = stack.app("college").state.sync_thread
            assert thread is not None and thread.thread.is_alive()
            register(stack, registration, [1, 2])
            deadline = time.time() + 15
            while stack.scalar("central", "SELECT count(*) FROM activity_events") < 2 and time.time() < deadline:
                time.sleep(0.05)
            assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 2   # synced with nobody asking
        assert not thread.thread.is_alive()                              # leaving the app stopped it
        from backend.main import create_app
        with TestClient(create_app(stack.settings("central"))) as c:
            assert c.app.state.sync_thread is None                       # central does not run a venue worker
        with TestClient(create_app(stack.settings("hall", central_url=None))) as c:
            assert c.app.state.sync_thread is None                       # a venue with no central configured just runs locally
        assert TestClient(create_app(stack.settings("hall", central_url=None))).get("/health").json()["db"] == "up"

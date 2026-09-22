"""Phase 15: cross-location prerequisites and reconciliation (SYSTEM_SPEC 11.5, 11.7), on the REAL topology
(tests/sync_support.py: separate databases, real sync between them).

The rule, and the test that proves each line of it:

    present locally                          -> ALLOW          TestTheRule::test_present_locally_allows...
    missing, owning venue FRESH              -> BLOCK          TestTheRule::test_missing_and_the_owner_fresh_is_blocked...
    missing, owning venue STALE              -> PROVISIONAL    TestTheRule::test_missing_and_the_owner_stale_is_accepted_provisionally...
      arrives later by sync                  -> exception auto-closes      TestProvisionalLifecycle::test_the_missing_record_arrives...
      never arrives                          -> OPEN exception stays       TestProvisionalLifecycle::test_a_truly_unregistered_student...
    genuine duplicate from a peer            -> stored, CONFLICT           TestConflicts
    same-venue prerequisites                 -> never affected             TestSameVenueRegression (one parametrized regression test)

The Phase 6 stub is gone: tests/test_station_engine.py::TestCrossVenueHook::test_the_phase_6_stub_is_gone... checks
the source, and every test below would fail against a hook that always allowed.
"""
import json

import pytest
from sqlalchemy import text

from backend.engine import messages
from backend.sync import reconcile as reconcile_svc
from backend.sync import status as sync_status
from backend.sync.ingest import ingest_events
from tests.sync_support import Stack
from tests.test_sync import author, central_client, payloads, register, unsent


@pytest.fixture(scope="module")
def stack():
    s = Stack("recon", students=40)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_slate(stack):
    stack.reset()
    yield
    stack.stop_central()


def allocation_station(stack):
    return stack.operator("stadium", "THOBE_ALLOCATION", "THO-01")


def return_station(stack):
    return stack.operator("hall", "THOBE_RETURN", "RET-01")


def events(stack, venue, i, activity):
    return stack.q(venue, "SELECT * FROM activity_events WHERE student_id = :s AND activity = :a ORDER BY venue_seq",
                   s=stack.student_id(i), a=activity)


def exceptions(stack, venue, type_="PROVISIONAL_UNCONFIRMED", status=None):
    sql = "SELECT * FROM exceptions WHERE type = :t" + (" AND status = :s" if status else "") + " ORDER BY id"
    return stack.q(venue, sql, t=type_, s=status)


def sync_all(stack):
    """One full round on every venue: push, then pull (twice, so what one venue pushed reaches all the others)."""
    for _ in range(2):
        for venue in ("college", "stadium", "hall"):
            assert stack.worker(venue).cycle().ok, venue


def scan_log(stack, venue, i):
    return [r["result"] for r in stack.q(venue, "SELECT result FROM scan_log WHERE student_id = :s ORDER BY id", s=stack.student_id(i))]


# =========================================================================== THE RULE
class TestTheRule:
    def test_present_locally_allows_with_no_provisional_flag_even_when_the_peer_is_stale(self, stack):
        stack.start_central()
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [1])
        sync_all(stack)
        assert len(events(stack, "stadium", 1, "REGISTRATION")) == 1              # the College's registration is HERE, as history
        stack.set_peer_freshness("stadium", "college", None)                     # ... and the College is now completely stale
        station = allocation_station(stack)
        assert stack.scan(station, "THO-01", 1)["result"] == "READY"
        done = stack.confirm(station, "THO-01", 1)
        assert done["result"] == "CONFIRMED"
        assert events(stack, "stadium", 1, "THOBE_ALLOCATION")[0]["flags"] == []          # present locally: a normal, unflagged event
        assert scan_log(stack, "stadium", 1) == ["READY", "SUCCESS"]        # the scan, then the confirm
        assert exceptions(stack, "stadium") == []

    def test_missing_and_the_owner_fresh_is_blocked_with_a_plain_message(self, stack):
        stack.start_central()
        assert stack.worker("college").cycle().ok and stack.worker("stadium").cycle().ok      # REAL sync: the College is known current
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_is_fresh(c, "college")
        station = allocation_station(stack)
        for body in (stack.scan(station, "THO-01", 2), stack.confirm(station, "THO-01", 2)):
            assert body["result"] == "REJECTED" and body["colour"] == "red"
            assert body["message"] == "THOBE NOT AVAILABLE — REGISTRATION PENDING"
        assert events(stack, "stadium", 2, "THOBE_ALLOCATION") == [] and exceptions(stack, "stadium") == []
        assert scan_log(stack, "stadium", 2) == ["REJECTED", "REJECTED"]

    def test_the_hall_blocks_each_missing_stadium_prerequisite_with_its_own_sentence_when_the_stadium_is_fresh(self, stack):
        stack.set_peer_freshness("hall", "stadium", 3)
        station = return_station(stack)
        assert stack.scan(station, "RET-01", 3)["message"] == "THOBE RETURN NOT AVAILABLE — STAGE PENDING"
        with stack.engine("hall").begin() as c:                                         # only the Stage record is present at the Hall
            c.execute(text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id) "
                           "VALUES (:s, 'STAGE', 'COMPLETE', 'stadium', 'SEED', gen_random_uuid())"), {"s": stack.student_id(3)})
        body = stack.scan(station, "RET-01", 3)
        assert body["result"] == "REJECTED" and body["message"] == "THOBE RETURN NOT AVAILABLE — NO THOBE WAS ISSUED"

    def test_missing_and_the_owner_stale_is_accepted_provisionally_without_alarming_the_operator(self, stack):
        station = allocation_station(stack)                                        # this server has never synced: everything is stale
        ready = stack.scan(station, "THO-01", 4)
        done = stack.confirm(station, "THO-01", 4)
        assert ready["result"] == "READY" and done["result"] == "CONFIRMED" and done["colour"] == "green"
        assert done["message"] == messages.CONFIRMED                               # exactly the ordinary confirmation
        for word in ("provisional", "stale", "sync", "pending", "warning", "unconfirmed"):
            assert word not in (json.dumps(ready) + json.dumps(done)).lower(), word
        event = events(stack, "stadium", 4, "THOBE_ALLOCATION")[0]
        assert event["flags"] == ["PROVISIONAL"] and event["kind"] == "COMPLETE"
        assert scan_log(stack, "stadium", 4) == ["READY", "PROVISIONAL"]    # the scan, then the confirm
        [item] = exceptions(stack, "stadium")
        assert item["status"] == "OPEN" and item["event_id"] == event["event_id"] and item["student_id"] == stack.student_id(4)
        assert item["details"]["missing"] == ["REGISTRATION"] and item["details"]["waiting_for_venues"] == ["college"]
        assert stack.scalar("stadium", "SELECT count(*) FROM outbox WHERE event_id = :e", e=event["event_id"]) == 1   # queued for sync as usual

    def test_the_window_boundary_and_a_configurable_window(self, stack):
        station = allocation_station(stack)
        window = 120
        stack.set_peer_freshness("stadium", "college", window - 5)                 # inside the window: fresh
        assert stack.scan(station, "THO-01", 5)["result"] == "REJECTED"
        stack.set_peer_freshness("stadium", "college", window + 5)                 # just outside: stale
        assert stack.scan(station, "THO-01", 5)["result"] == "READY"
        with stack.engine("stadium").begin() as c:                                 # a longer window makes the same peer fresh again
            c.execute(text("UPDATE settings SET freshness_window_seconds = 300"))
        assert stack.scan(station, "THO-01", 5)["result"] == "REJECTED"
        with stack.engine("stadium").begin() as c:                                 # a shorter one makes a young peer stale
            c.execute(text("UPDATE settings SET freshness_window_seconds = 10"))
        stack.set_peer_freshness("stadium", "college", 30)
        assert stack.scan(station, "THO-01", 5)["result"] == "READY"

    def test_a_peer_that_has_never_been_synced_counts_as_stale_never_as_fresh(self, stack):
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_age_seconds(c, "college") is None and sync_status.peer_is_fresh(c, "college") is False
        assert stack.scan(allocation_station(stack), "THO-01", 6)["result"] == "READY"


# =========================================================================== THE PROVISIONAL LIFECYCLE
class TestProvisionalLifecycle:
    def test_the_missing_record_arrives_by_sync_and_the_exception_closes_itself(self, stack):
        stack.start_central()
        station = allocation_station(stack)
        assert stack.confirm(station, "THO-01", 7)["result"] == "CONFIRMED"           # the Stadium is cut off from the College: provisional
        event = events(stack, "stadium", 7, "THOBE_ALLOCATION")[0]
        assert exceptions(stack, "stadium", status="OPEN")[0]["event_id"] == event["event_id"]

        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [7])      # meanwhile the College did register them
        sync_all(stack)                                                                # connectivity returns; everyone syncs

        [item] = exceptions(stack, "stadium")
        assert item["status"] == "RESOLVED" and item["resolved_by"] is None            # closed by the SYSTEM, not a person
        assert "arrived by sync" in item["resolution_note"]
        assert stack.scalar("stadium", "SELECT count(*) FROM audit_log WHERE action = 'EXCEPTION_AUTO_CLOSED' AND event_id = :e", e=event["event_id"]) == 1
        assert events(stack, "stadium", 7, "THOBE_ALLOCATION")[0]["flags"] == ["PROVISIONAL"]     # history is not rewritten
        assert exceptions(stack, "central", status="OPEN") == []                       # and central, which now holds both, has nothing to flag
        assert stack.scalar("stadium", "SELECT count(*) FROM exceptions WHERE status = 'OPEN'") == 0

    def test_a_truly_unregistered_student_leaves_an_open_exception_after_reconciliation(self, stack):
        stack.start_central()
        assert stack.confirm(allocation_station(stack), "THO-01", 8)["result"] == "CONFIRMED"     # student 8 never registers anywhere
        sync_all(stack)                                                                            # everything is now current everywhere
        with stack.engine("stadium").connect() as c:
            assert sync_status.peer_is_fresh(c, "college")                                          # the College is fresh and does NOT have them
        [here] = exceptions(stack, "stadium")
        assert here["status"] == "OPEN" and here["details"]["missing"] == ["REGISTRATION"]
        [there] = exceptions(stack, "central")                                                      # central raises it too, on the event as it arrived
        assert there["status"] == "OPEN" and there["event_id"] == here["event_id"]
        for _ in range(2):                                                                          # reconciling again changes nothing
            assert reconcile_svc.reconcile(stack.engine("stadium"))["provisional_opened"] == 0
            assert reconcile_svc.reconcile(stack.engine("central"))["provisional_opened"] == 0
        assert len(exceptions(stack, "stadium")) == 1 and len(exceptions(stack, "central")) == 1
        health = stack.admin("stadium").get("/admin/api/dashboard").json()
        assert health["exceptions"]["open_by_type"].get("PROVISIONAL_UNCONFIRMED") == 1 and health["exceptions"]["provisional_events"] == 1
        # an Admin who reviews and resolves it by hand is never overruled by the system
        assert stack.admin("stadium").post(f"/admin/api/exceptions/{here['id']}/resolve", json={"note": "checked the paper list: they did register"}).status_code == 200
        reconcile_svc.reconcile(stack.engine("stadium"))
        assert [x["status"] for x in exceptions(stack, "stadium")] == ["RESOLVED"]

    def test_the_hall_accepts_a_return_provisionally_when_the_stadiums_events_have_not_arrived(self, stack):
        stack.start_central()
        for a in ("THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"):                    # the Stadium recorded the whole stage side
            author(stack, "stadium", 9, a, outbox=True)
        author(stack, "college", 9, "REGISTRATION", outbox=True)
        station = return_station(stack)                                                # ... but the Hall has not heard yet
        done = stack.confirm(station, "RET-01", 9)
        assert done["result"] == "CONFIRMED" and done["message"] == messages.CONFIRMED
        event = events(stack, "hall", 9, "THOBE_RETURN")[0]
        assert event["flags"] == ["PROVISIONAL"]
        [item] = exceptions(stack, "hall")
        assert item["status"] == "OPEN" and item["details"]["missing"] == ["STAGE", "THOBE_ALLOCATION"] and item["details"]["waiting_for_venues"] == ["stadium"]
        sync_all(stack)
        assert [x["status"] for x in exceptions(stack, "hall")] == ["RESOLVED"]         # both arrived: closed

    def test_a_provisional_return_stays_open_while_only_part_of_what_it_waited_for_has_arrived(self, stack):
        stack.start_central()
        author(stack, "stadium", 10, "THOBE_ALLOCATION", outbox=True)                  # the allocation reaches the Hall, the Stage record never does
        assert stack.confirm(return_station(stack), "RET-01", 10)["result"] == "CONFIRMED"
        [item] = exceptions(stack, "hall")
        assert item["details"]["missing"] == ["STAGE", "THOBE_ALLOCATION"]
        sync_all(stack)
        assert [(x["status"]) for x in exceptions(stack, "hall")] == ["OPEN"]           # Stage is still missing: still for the Admin
        author(stack, "stadium", 10, "STAGE", outbox=True)                              # ... until it finally does arrive
        sync_all(stack)
        assert [x["status"] for x in exceptions(stack, "hall")] == ["RESOLVED"]

    def test_a_provisional_record_an_admin_reverses_needs_no_more_review(self, stack):
        stack.start_central()
        station = allocation_station(stack)
        assert stack.confirm(station, "THO-01", 20)["result"] == "CONFIRMED"
        event = events(stack, "stadium", 20, "THOBE_ALLOCATION")[0]
        assert [x["status"] for x in exceptions(stack, "stadium")] == ["OPEN"]
        r = stack.admin("stadium").post("/admin/api/corrections/reverse", json={"event_id": str(event["event_id"]), "reason": "wrong student"})
        assert r.status_code == 200, r.text
        assert stack.admin("stadium").post("/admin/api/reconcile").json()["provisional_closed"] == 1
        [item] = exceptions(stack, "stadium")
        assert item["status"] == "RESOLVED" and item["resolved_by"] is None and "reversed by an Admin" in item["resolution_note"]

    def test_provisional_items_show_on_the_admin_dashboard_and_in_the_provisional_report(self, stack):
        assert stack.confirm(allocation_station(stack), "THO-01", 11)["result"] == "CONFIRMED"
        admin = stack.admin("stadium")
        dash = admin.get("/admin/api/dashboard").json()["exceptions"]
        assert dash["provisional_events"] == 1 and dash["open"] == 1
        rows = admin.get("/admin/api/reports/provisional").json()["rows"]
        assert [r["prn"] for r in rows] == ["P0011"] and rows[0]["flags"] == "PROVISIONAL"
        listing = admin.get("/admin/api/exceptions", params={"type": "PROVISIONAL_UNCONFIRMED"}).json()["exceptions"]
        assert listing and listing[0]["status"] == "OPEN" and listing[0]["prn"] == "P0011"

    def test_reconcile_can_be_run_on_demand_by_an_admin_only(self, stack):
        assert stack.confirm(allocation_station(stack), "THO-01", 12)["result"] == "CONFIRMED"
        first = stack.admin("stadium").post("/admin/api/reconcile").json()
        assert first["provisional_opened"] == 0 and first["gaps_opened"] == 0            # nothing new: it was raised when accepted
        assert stack.admin("stadium").post("/admin/api/reconcile").json() == first        # idempotent
        assert allocation_station(stack).post("/admin/api/reconcile").status_code == 403


# =========================================================================== CONFLICTS
class TestConflicts:
    def _duplicate_pair(self, stack):
        """A second, different Thobe Allocation for the same student, as a promoted standby that also accepted it would send."""
        first = author(stack, "stadium", 13, "THOBE_ALLOCATION", outbox=True)
        [original] = [r["p"] for r in payloads(stack, "stadium")]
        rival = {**original, "event_id": "aaaaaaaa-0000-4000-8000-000000000001", "venue_seq": original["venue_seq"] + 1}
        return first, original, rival

    def test_a_genuine_duplicate_from_a_peer_is_stored_and_flagged_never_merged(self, stack):
        stack.start_central()
        first, original, rival = self._duplicate_pair(stack)
        client = central_client(stack, "stadium")
        answer = client.push([original, rival], pending_after=0, last_seq=2, drain_total=0, drain_done=0)
        assert [r["status"] for r in answer["results"]] == ["ACCEPTED", "CONFLICT"]
        # NOT merged: central still has exactly one completion, untouched
        held = stack.q("central", "SELECT event_id::text AS e FROM activity_events WHERE student_id = :s", s=stack.student_id(13))
        assert [h["e"] for h in held] == [first]
        assert stack.scalar("central", "SELECT status FROM student_status WHERE student_id = :s", s=stack.student_id(13)) == "NOT SEATED"
        # NOT dropped: the whole incoming event is kept
        [kept] = stack.q("central", "SELECT * FROM conflict_events")
        assert str(kept["event_id"]) == rival["event_id"] and kept["payload"] == rival and str(kept["existing_event_id"]) == first
        assert "second, different completion" in kept["reason"] and kept["venue_id"] == "stadium"
        # FLAGGED: an OPEN CONFLICT exception for the Admin, and an audit row
        [item] = exceptions(stack, "central", "CONFLICT")
        assert item["status"] == "OPEN" and str(item["event_id"]) == rival["event_id"] and item["details"]["existing_event_id"] == first
        assert stack.scalar("central", "SELECT count(*) FROM audit_log WHERE action = 'SYNC_CONFLICT'") == 1
        # sending it again changes nothing: one conflict row, one exception
        again = client.push([rival], pending_after=0, last_seq=2, drain_total=0, drain_done=0)
        assert again["results"][0]["status"] == "CONFLICT"
        assert stack.scalar("central", "SELECT count(*) FROM conflict_events") == 1 and len(exceptions(stack, "central", "CONFLICT")) == 1
        assert stack.admin("central").get("/admin/api/dashboard").json()["health"]["conflicts"] == 1

    def test_the_sender_marks_a_stored_conflict_as_delivered_and_does_not_resend_it(self, stack):
        stack.start_central()
        first, original, rival = self._duplicate_pair(stack)
        with stack.engine("stadium").begin() as c:                                     # the rival is in the Stadium's own outbox
            c.execute(text("INSERT INTO outbox (event_id, payload) VALUES (:e, CAST(:p AS jsonb))"), {"e": rival["event_id"], "p": json.dumps(rival)})
        assert stack.worker("stadium").cycle().ok
        assert unsent(stack, "stadium") == 0                                            # central holds it (as a conflict): nothing left to send
        assert stack.scalar("central", "SELECT count(*) FROM conflict_events") == 1 and stack.scalar("central", "SELECT count(*) FROM activity_events") == 1

    def test_a_reused_event_id_or_a_reused_venue_seq_is_also_a_conflict(self, stack):
        stack.start_central()
        first, original, rival = self._duplicate_pair(stack)
        client = central_client(stack, "stadium")
        client.push([original], pending_after=0, last_seq=1, drain_total=0, drain_done=0)
        same_id_other_content = {**original, "student_id": str(stack.student_id(14))}               # same event_id, a different student
        same_seq_other_event = {**original, "event_id": "aaaaaaaa-0000-4000-8000-000000000002", "student_id": str(stack.student_id(15))}
        answer = client.push([same_id_other_content, same_seq_other_event], pending_after=0, last_seq=1, drain_total=0, drain_done=0)
        assert [r["status"] for r in answer["results"]] == ["CONFLICT", "CONFLICT"]
        reasons = sorted(r["reason"] for r in stack.q("central", "SELECT reason FROM conflict_events"))
        assert any("different content" in r for r in reasons) and any("venue number" in r for r in reasons)
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 1                # still only the original

    def test_a_venue_applying_a_peers_duplicate_flags_it_too(self, stack):
        first, original, rival = self._duplicate_pair(stack)
        with stack.engine("hall").begin() as c:                                            # the Hall pulls both from central
            results = ingest_events(c, [original, rival], skip_venue="hall", source="pull")
        assert [r.status for r in results] == ["ACCEPTED", "CONFLICT"]
        assert len(exceptions(stack, "hall", "CONFLICT", "OPEN")) == 1 and stack.scalar("hall", "SELECT count(*) FROM conflict_events") == 1
        assert stack.scalar("hall", "SELECT count(*) FROM activity_events") == 1

    def test_conflict_evidence_cannot_be_edited_or_deleted(self, stack):
        first, original, rival = self._duplicate_pair(stack)
        with stack.engine("hall").begin() as c:
            ingest_events(c, [original, rival], skip_venue="hall")
        for sql in ("UPDATE conflict_events SET reason = 'edited'", "DELETE FROM conflict_events", "TRUNCATE conflict_events"):
            with stack.engine("hall").connect() as c:
                with pytest.raises(Exception) as exc:
                    c.execute(text(sql))
                assert "append-only" in str(exc.value) or "23001" in str(getattr(exc.value.orig, "pgcode", ""))


# =========================================================================== SAME-VENUE REGRESSION
class TestSameVenueRegression:
    """Same-venue prerequisites are hard blocks. Nothing about freshness, staleness, sync or provisional acceptance
    may ever reach them. One parametrized test: every same-venue prerequisite x every possible sync state."""

    CASES = [  # activity, venue, station, missing prerequisite (same venue), the configured hard-block sentence
        ("SEATING", "stadium", "SEA-01", "THOBE_ALLOCATION", "SEATING NOT AVAILABLE — THOBE NOT RECEIVED"),
        ("QUEUE", "stadium", "QUE-01", "SEATING", "QUEUE NOT AVAILABLE — SEATING PENDING"),
        ("STAGE", "stadium", "STG-01", "QUEUE", "STAGE NOT AVAILABLE — QUEUE PENDING"),
        ("LUNCH", "hall", "LUN-01", "THOBE_RETURN", "LUNCH NOT AVAILABLE — THOBE RETURN PENDING"),
    ]
    SYNC_STATES = {"never synced": None, "stale (10 minutes)": 600, "fresh (1 second)": 1}

    @pytest.mark.parametrize("state", list(SYNC_STATES))
    @pytest.mark.parametrize("activity,venue,station_id,missing,sentence", CASES)
    def test_a_missing_same_venue_prerequisite_is_always_a_hard_block_and_never_provisional(
            self, stack, activity, venue, station_id, missing, sentence, state):
        age = self.SYNC_STATES[state]
        for peer in ("college", "stadium", "hall"):
            if peer != venue:
                stack.set_peer_freshness(venue, peer, age)                            # EVERY peer in the same state
        operator = stack.operator(venue, activity, station_id)
        i = 16 + [c[0] for c in self.CASES].index(activity)
        # (each of these activities has ONE prerequisite, and it is the same-venue one: that is what is missing)
        before = stack.scalar(venue, "SELECT count(*) FROM activity_events")
        for body in (stack.scan(operator, station_id, i), stack.confirm(operator, station_id, i)):
            assert body["result"] == "REJECTED" and body["message"] == sentence, (activity, state, body)
        assert stack.scalar(venue, "SELECT count(*) FROM activity_events") == before                        # nothing recorded
        assert exceptions(stack, venue) == []                                                               # no provisional exception
        assert set(scan_log(stack, venue, i)) == {"REJECTED"}                                               # never PROVISIONAL
        assert stack.scalar(venue, "SELECT count(*) FROM activity_events WHERE 'PROVISIONAL' = ANY (flags)") == 0

    def test_same_venue_prerequisites_present_means_ready_and_unflagged_whatever_the_sync_state(self, stack):
        stack.set_peer_freshness("stadium", "college", None)
        station = stack.operator("stadium", "SEATING", "SEA-01")
        author(stack, "stadium", 30, "THOBE_ALLOCATION")                                                   # the same-venue prerequisite is present
        assert stack.confirm(station, "SEA-01", 30)["result"] == "CONFIRMED"
        assert events(stack, "stadium", 30, "SEATING")[0]["flags"] == [] and exceptions(stack, "stadium") == []

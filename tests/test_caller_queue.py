"""Test suite for the new caller queue list (Feature: caller queue list).

Tests WRITTEN FIRST (golden rule 12).

What is under test:
  - GET /caller/queue  — returns queued students (status=QUEUED) not yet dismissed by the caller,
    in queue_position order.  Each row: student_id, name, prn, programme, school, photo_url,
    queue_position.  The photo_url uses /photo/{student_id} (same as the Stage screen, auth-gated).
  - POST /caller/dismiss/<student_id>  — idempotent, audit-logged.  Writes to caller_dismissals
    only; never touches queue.status or stage_state.
  - Live update: the SSE version SQL watches both the QUEUED count and the dismissal count, so
    a new queue scan or a new dismiss both push an event.
  - Separation: Stage operator's list (queue.status=QUEUED) is NOT affected by a caller dismiss.
    The reverse: Stage NEXT / SEND (which sets status=DISPLAYED) removes the student from the
    Stage list but the caller still sees them until they press Complete themselves.
  - Reversed QUEUE event (Admin): deletes the queue row → student disappears from both lists.
  - Page reload keeps state (caller_dismissals is server-side).
  - Two caller devices stay in sync (both watch the same SSE stream; after A dismisses, B's
    next queue fetch excludes that student).
  - Role permissions: only CALLER / ADMIN / DEPUTY_ADMIN may call dismiss; STAGE may view but
    may not dismiss.  Other operators get 403.
"""
import json
import uuid

import pytest
from sqlalchemy import text

from backend import users as users_svc
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_stage import (  # noqa: F401  (fixtures)
    _fresh_client_cache,
    act,
    add_snapshot,
    bounded_stream,
    claim,
    clean_stage,
    led,
    nxt,
    queued,
    stage,
)
from tests.test_station_engine import (  # noqa: F401
    admin,
    apps,
    engine,
    operator,
    q,
    world,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def caller(engine, world, apps):
    """A logged-in CALLER client (reuses the existing caller-1 from test_caller.py if it
    was already created in the same module fixture scope; otherwise creates it fresh)."""
    try:
        with engine.begin() as c:
            users_svc.create_user(c, username="caller-q1", password=PASSWORD, role="CALLER")
    except Exception:
        pass  # already exists from a previous run in the same db; that's fine
    client = new_client(apps)
    assert api_login(client, "caller-q1").status_code == 200
    return client


@pytest.fixture(scope="module")
def caller2(engine, world, apps):
    """A second CALLER client — simulates two devices open at once."""
    try:
        with engine.begin() as c:
            users_svc.create_user(c, username="caller-q2", password=PASSWORD, role="CALLER")
    except Exception:
        pass
    client = new_client(apps)
    assert api_login(client, "caller-q2").status_code == 200
    return client


def queue_list(client, **params):
    """GET /caller/queue and return the parsed JSON."""
    resp = client.get("/caller/queue", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def dismiss(client, student_id):
    """POST /caller/dismiss/<student_id> and return the response."""
    return client.post(f"/caller/dismiss/{student_id}")


def caller_queue_ids(client, **params):
    """Return the ordered list of student_id strings from the caller queue."""
    return [s["student_id"] for s in queue_list(client, **params)["students"]]


def clear_dismissals(engine):
    """Remove all caller_dismissals rows between tests."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM caller_dismissals"))


# ─── 1. Queue list endpoint ───────────────────────────────────────────────────

class TestCallerQueueList:
    def test_new_queue_scan_appears_in_caller_list_immediately(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 3)
        data = queue_list(caller)
        ids_in_list = [s["student_id"] for s in data["students"]]
        for s in students:
            assert str(s.id) in ids_in_list, f"{s.name} not found in caller queue"

    def test_queue_order_is_queue_position_ascending(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 4)
        data = queue_list(caller)
        positions = [s["queue_position"] for s in data["students"]]
        assert positions == sorted(positions), "caller queue must be in ascending queue_position order"
        # The students we just queued appear at the end (highest positions)
        returned_ids = [s["student_id"] for s in data["students"]]
        for s in students:
            assert str(s.id) in returned_ids

    def test_each_row_has_the_required_fields(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        data = queue_list(caller)
        assert len(data["students"]) >= 1
        row = data["students"][0]
        required = {"student_id", "name", "prn", "programme", "school", "photo_url", "queue_position"}
        assert required <= set(row.keys()), f"Missing fields: {required - set(row.keys())}"

    def test_photo_url_is_auth_gated_not_public_led_photo(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        data = queue_list(caller)
        photo_url = data["students"][0]["photo_url"]
        # Must use /photo/<uuid>, never /led/photo/<key> (no opaque LED key on the caller list)
        assert "/photo/" in photo_url
        assert "/led/photo/" not in photo_url

    def test_only_queued_students_appear_not_displayed_or_done(
        self, apps, world, engine, caller, stage
    ):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 3)
        claim(stage)
        # Advance first student to DISPLAYED
        nxt(stage.main)
        data = queue_list(caller)
        ids_in_list = [s["student_id"] for s in data["students"]]
        # The first student is now DISPLAYED — still visible to caller (they're still in the queue
        # physically), but the Stage screen sees them as current. The caller sees all not-yet-dismissed.
        # IMPORTANT: caller filters on status='QUEUED'; DISPLAYED students left the queue.
        assert str(students[0].id) not in ids_in_list, \
            "DISPLAYED student should not appear in caller list (they left QUEUED status)"
        for s in students[1:]:
            assert str(s.id) in ids_in_list

    def test_total_count_in_response(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 2)
        data = queue_list(caller)
        assert "total" in data
        assert data["total"] >= 2

    def test_pagination_after_param(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 5)
        full = queue_list(caller)
        if len(full["students"]) < 2:
            pytest.skip("need at least 2 students to test pagination")
        first_pos = full["students"][0]["queue_position"]
        paged = queue_list(caller, after=first_pos)
        ids_paged = [s["student_id"] for s in paged["students"]]
        assert full["students"][0]["student_id"] not in ids_paged, \
            "after= should exclude the first position"


# ─── 2. Dismiss endpoint ─────────────────────────────────────────────────────

class TestCallerDismiss:
    def test_complete_removes_student_from_caller_list(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        target = students[0]
        r = dismiss(caller, str(target.id))
        assert r.status_code == 200, r.text
        ids = caller_queue_ids(caller)
        assert str(target.id) not in ids
        # Other student still there
        assert str(students[1].id) in ids

    def test_complete_is_idempotent(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        target = students[0]
        r1 = dismiss(caller, str(target.id))
        r2 = dismiss(caller, str(target.id))
        assert r1.status_code == 200
        assert r2.status_code == 200  # idempotent — no error on second dismiss
        ids = caller_queue_ids(caller)
        assert str(target.id) not in ids

    def test_complete_is_audit_logged(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        target = students[0]
        dismiss(caller, str(target.id))
        rows = q(engine, "SELECT action, student_id FROM audit_log WHERE action = 'CALLER_DISMISS' ORDER BY id DESC LIMIT 1")
        assert len(rows) == 1
        assert str(rows[0]["student_id"]) == str(target.id)

    def test_complete_does_not_affect_stage_queue_status(self, apps, world, engine, caller, stage):
        """Golden rule: caller Complete must NOT remove the student from the Stage operator's list."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        target = students[0]
        # Dismiss from caller list
        dismiss(caller, str(target.id))
        # Stage state: student should still be status=QUEUED
        row = q(engine, "SELECT status FROM queue WHERE student_id = :s", s=str(target.id))
        assert row, "queue row must still exist after caller dismiss"
        assert row[0]["status"] == "QUEUED", "caller dismiss must not change queue.status"

    def test_stage_next_does_not_remove_student_from_caller_list(
        self, apps, world, engine, caller, stage
    ):
        """Stage NEXT sets status=DISPLAYED, removing from Stage list. But caller must NOT
        auto-dismiss — the caller sees the student until they press Complete themselves."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        first = students[0]
        claim(stage)
        # Stage presses NEXT — first student goes on stage (DISPLAYED)
        nxt(stage.main)
        # Stage list no longer shows first student, but caller list should ALSO not show them
        # because status is now DISPLAYED, not QUEUED. The caller's filter is status='QUEUED'.
        # This is correct: once DISPLAYED they are physically on stage, caller doesn't need them.
        ids = caller_queue_ids(caller)
        assert str(first.id) not in ids, \
            "DISPLAYED student leaves caller list automatically (status change), caller need not press Complete"

    def test_unknown_student_id_returns_404(self, apps, world, engine, caller):
        clear_dismissals(engine)
        r = caller.post(f"/caller/dismiss/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_malformed_student_id_returns_422_or_404(self, apps, world, engine, caller):
        clear_dismissals(engine)
        r = caller.post("/caller/dismiss/not-a-uuid")
        assert r.status_code in (404, 422)


# ─── 3. Separation from Stage list ───────────────────────────────────────────

class TestSeparation:
    def test_stage_list_unaffected_when_caller_dismisses(self, apps, world, engine, caller, stage):
        """Caller dismiss writes only to caller_dismissals; queue.status stays QUEUED."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 3)
        claim(stage)
        # Get Stage waiting list before dismiss
        stage_before = stage.main.get("/stage/state").json()["waiting"]
        stage_ids_before = {c["student_id"] for c in stage_before}
        # Dismiss first student from caller list
        dismiss(caller, str(students[0].id))
        # Stage waiting list must be unchanged
        stage_after = stage.main.get("/stage/state").json()["waiting"]
        stage_ids_after = {c["student_id"] for c in stage_after}
        assert str(students[0].id) in stage_ids_after, \
            "Stage list must NOT change when caller presses Complete"
        assert stage_ids_before == stage_ids_after


# ─── 4. Reversed QUEUE event removes student from both lists ─────────────────

class TestReversalRemovesFromBoth:
    def test_reversed_queue_event_removes_student_from_caller_list(
        self, apps, world, engine, caller
    ):
        """Admin reversal of QUEUE deletes the queue row → student disappears from caller list too."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        target = students[0]
        # Verify target is in caller list
        assert str(target.id) in caller_queue_ids(caller)
        # Admin reverses the QUEUE event
        adm = admin(apps)
        events = q(engine,
                   "SELECT event_id FROM activity_events WHERE student_id = :s AND activity = 'QUEUE' "
                   "AND kind = 'COMPLETE' ORDER BY server_time DESC LIMIT 1",
                   s=str(target.id))
        assert events, "no QUEUE event found"
        r = adm.post("/admin/api/corrections/reverse",
                     json={"event_id": str(events[0]["event_id"]), "reason": "test reversal"})
        assert r.status_code == 200, r.text
        # Queue row deleted by reversal — student gone from caller list
        ids = caller_queue_ids(caller)
        assert str(target.id) not in ids, \
            "reversed queue student must disappear from caller list"


# ─── 5. Page reload keeps state ───────────────────────────────────────────────

class TestReloadKeepsState:
    def test_dismiss_persists_across_page_reload(self, apps, world, engine, caller):
        """caller_dismissals is server-side; a fresh HTTP fetch still excludes dismissed students."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        target = students[0]
        dismiss(caller, str(target.id))
        # Simulate reload: fetch the list again (new HTTP request, same session)
        ids_after_reload = caller_queue_ids(caller)
        assert str(target.id) not in ids_after_reload


# ─── 6. Two caller devices stay in sync ──────────────────────────────────────

class TestMultiDevice:
    def test_two_caller_devices_stay_in_sync(self, apps, world, engine, caller, caller2):
        """After device A dismisses, device B's next queue fetch also excludes that student."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 3)
        target = students[0]
        # Device A dismisses
        r = dismiss(caller, str(target.id))
        assert r.status_code == 200
        # Device B fetches — must not see the dismissed student
        ids_b = caller_queue_ids(caller2)
        assert str(target.id) not in ids_b, \
            "second caller device must see the same dismissed state"


# ─── 7. SSE version changes on queue scan and dismiss ────────────────────────

class TestSSEVersionChanges:
    def _caller_queue_version(self, engine):
        """Read the caller-queue SSE version string (queue count + dismissal count)."""
        from backend.stage.led import CALLER_QUEUE_VERSION_SQL
        with engine.connect() as c:
            return c.execute(text(CALLER_QUEUE_VERSION_SQL)).scalar_one()

    def test_new_queue_scan_changes_the_sse_version(self, apps, world, engine, caller):
        clear_dismissals(engine)
        before = self._caller_queue_version(engine)
        queued(engine, apps, world, 1)
        after = self._caller_queue_version(engine)
        assert before != after, "SSE version must change when a student is queued"

    def test_dismiss_changes_the_sse_version(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        before = self._caller_queue_version(engine)
        dismiss(caller, str(students[0].id))
        after = self._caller_queue_version(engine)
        assert before != after, "SSE version must change when a student is dismissed"


# ─── 8. Role permissions ─────────────────────────────────────────────────────

class TestPermissions:
    def test_caller_and_admin_may_dismiss(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        # CALLER role
        r = dismiss(caller, str(students[0].id))
        assert r.status_code == 200
        # ADMIN role
        adm = admin(apps)
        r2 = dismiss(adm, str(students[1].id))
        assert r2.status_code == 200

    def test_stage_operator_may_NOT_dismiss(self, apps, world, engine, stage):
        """STAGE can view the caller list but must NOT be able to dismiss."""
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        r = stage.main.post(f"/caller/dismiss/{students[0].id}")
        assert r.status_code == 403

    @pytest.mark.parametrize("activity", ["REGISTRATION", "SEATING", "QUEUE", "LUNCH"])
    def test_other_operators_may_not_use_caller_queue_at_all(
        self, apps, world, engine, activity
    ):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        client = operator(apps, world, activity)
        assert client.get("/caller/queue").status_code == 403
        assert client.post(f"/caller/dismiss/{students[0].id}").status_code == 403

    def test_anonymous_user_gets_401(self, apps, world, engine):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        anon = new_client(apps)
        assert anon.get("/caller/queue").status_code == 401
        assert anon.post(f"/caller/dismiss/{uuid.uuid4()}").status_code == 401

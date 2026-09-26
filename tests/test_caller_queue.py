"""Test suite for the new caller queue list (Feature: caller queue list).

Tests WRITTEN FIRST (golden rule 12).

What is under test:
  - GET /caller/queue  — returns queued students (status=QUEUED) not yet dismissed by the caller,
    in queue_position order.  Each row: student_id, name, prn, programme, school, photo_url,
    queue_position.  The photo_url uses /photo/{student_id}.
  - POST /caller/dismiss/<student_id>  — idempotent, audit-logged.
  - POST /caller/next/<student_id> — Same as dismiss.
  - Reversed QUEUE event (Admin): deletes the queue row → student disappears from lists.
  - Page reload keeps state (caller_dismissals is server-side).
  - Two caller devices stay in sync (both watch the same SSE stream; after A dismisses, B's
    next queue fetch excludes that student).
  - Role permissions: only CALLER / ADMIN / DEPUTY_ADMIN may view the list or call NEXT.
"""
import uuid

import pytest
from sqlalchemy import text

from backend import users as users_svc
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_station_engine import (  # noqa: F401
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    make_student,
    operator,
    q,
    seed_events,
    world,
)

@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()  # clients cached by other modules belong to a database that no longer exists
    yield
    _CLIENTS.clear()


# ─── helpers ──────────────────────────────────────────────────────────────────

def queued(engine, apps, world, count=1):
    """N students put through the real Queue scan point (Reporting and the robe seeded first)."""
    students = [make_student(engine) for _ in range(count)]
    queue_op = operator(apps, world, "QUEUE")
    for s in students:
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION"])
        assert confirm(queue_op, "QUEUE", token=s.token).json()["result"] == "CONFIRMED"
    return students

@pytest.fixture(scope="module")
def caller(engine, world, apps):
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
    try:
        with engine.begin() as c:
            users_svc.create_user(c, username="caller-q2", password=PASSWORD, role="CALLER")
    except Exception:
        pass
    client = new_client(apps)
    assert api_login(client, "caller-q2").status_code == 200
    return client

def queue_list(client, **params):
    resp = client.get("/caller/queue", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()

def dismiss(client, student_id):
    return client.post(f"/caller/next/{student_id}")

def caller_queue_ids(client, **params):
    return [s["student_id"] for s in queue_list(client, **params)["students"]]

def clear_dismissals(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE queue SET called_at = NULL"))

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

    def test_each_row_has_the_required_fields(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        data = queue_list(caller)
        assert len(data["students"]) >= 1
        row = data["students"][0]
        required = {"student_id", "name", "prn", "programme", "school", "photo_url", "queue_position"}
        assert required <= set(row.keys()), f"Missing fields: {required - set(row.keys())}"

    def test_photo_url_is_the_auth_gated_photo_route(self, apps, world, engine, caller):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        data = queue_list(caller)
        photo_url = data["students"][0]["photo_url"]
        assert photo_url.startswith("/photo/")

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
        assert str(students[1].id) in ids

    def test_complete_is_idempotent(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        target = students[0]
        r1 = dismiss(caller, str(target.id))
        r2 = dismiss(caller, str(target.id))
        assert r1.status_code == 200
        assert r2.status_code == 200
        ids = caller_queue_ids(caller)
        assert str(target.id) not in ids

    def test_complete_is_audit_logged(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        target = students[0]
        dismiss(caller, str(target.id))
        rows = q(engine, "SELECT action, student_id FROM audit_log WHERE action = 'CALLER_NEXT' ORDER BY id DESC LIMIT 1")
        assert len(rows) == 1
        assert str(rows[0]["student_id"]) == str(target.id)

    def test_unknown_student_id_returns_404(self, apps, world, engine, caller):
        clear_dismissals(engine)
        r = caller.post(f"/caller/next/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_malformed_student_id_returns_422_or_404(self, apps, world, engine, caller):
        clear_dismissals(engine)
        r = caller.post("/caller/next/not-a-uuid")
        assert r.status_code in (404, 422)

# ─── 4. Reversed QUEUE event removes student from both lists ─────────────────

class TestReversalRemovesFromBoth:
    def test_reversed_queue_event_removes_student_from_caller_list(
        self, apps, world, engine, caller
    ):
        clear_dismissals(engine)
        target = queued(engine, apps, world, 1)[0]  # a real Queue scan, so there is an event to reverse

        assert str(target.id) in caller_queue_ids(caller)
        adm = admin(apps)
        events = q(engine,
                   "SELECT event_id FROM activity_events WHERE student_id = :s AND activity = 'QUEUE' "
                   "AND kind = 'COMPLETE' ORDER BY server_time DESC LIMIT 1",
                   s=str(target.id))
        assert events, "no QUEUE event found"
        r = adm.post("/admin/api/corrections/reverse",
                     json={"event_id": str(events[0]["event_id"]), "reason": "test reversal"})
        assert r.status_code == 200, r.text
        ids = caller_queue_ids(caller)
        assert str(target.id) not in ids

# ─── 5. Page reload keeps state ───────────────────────────────────────────────

class TestReloadKeepsState:
    def test_dismiss_persists_across_page_reload(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        target = students[0]
        dismiss(caller, str(target.id))
        ids_after_reload = caller_queue_ids(caller)
        assert str(target.id) not in ids_after_reload

# ─── 6. Two caller devices stay in sync ──────────────────────────────────────

class TestMultiDevice:
    def test_two_caller_devices_stay_in_sync(self, apps, world, engine, caller, caller2):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 3)
        target = students[0]
        r = dismiss(caller, str(target.id))
        assert r.status_code == 200
        ids_b = caller_queue_ids(caller2)
        assert str(target.id) not in ids_b

# ─── 8. Role permissions ─────────────────────────────────────────────────────

class TestPermissions:
    def test_caller_and_admin_may_dismiss(self, apps, world, engine, caller):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 2)
        r = dismiss(caller, str(students[0].id))
        assert r.status_code == 200
        adm = admin(apps)
        r2 = dismiss(adm, str(students[1].id))
        assert r2.status_code == 200

    @pytest.mark.parametrize("activity", ["REGISTRATION", "SEATING", "QUEUE", "LUNCH"])
    def test_other_operators_may_not_use_caller_queue_at_all(
        self, apps, world, engine, activity
    ):
        clear_dismissals(engine)
        students = queued(engine, apps, world, 1)
        client = operator(apps, world, activity)
        assert client.get("/caller/queue").status_code == 403
        assert client.post(f"/caller/next/{students[0].id}").status_code == 403

    def test_anonymous_user_gets_401(self, apps, world, engine):
        clear_dismissals(engine)
        queued(engine, apps, world, 1)
        anon = new_client(apps)
        assert anon.get("/caller/queue").status_code == 401
        assert anon.post(f"/caller/next/{uuid.uuid4()}").status_code == 401

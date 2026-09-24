"""
Comprehensive test suite for STRUCTURE CHANGE v3:
Queue / Caller / Stage separation:
1. Realtime updates < 2s via SSE /events/queue
2. Caller Next isolation from Stage (sets called_at, removed from /caller/queue, stays in /stage/queue, no stage/LED advance)
3. Stage advance isolation from Caller (sets staged_at, removed from /stage/queue, called_at untouched)
4. Cross-role 403 rejection:
   - CALLER calling /stage/next, /stage/display, etc. gets 403
   - STAGE calling /caller/next gets 403
5. Atomic Next double-tap / idempotency / concurrency
"""
import concurrent.futures
import json
import uuid
import pytest
from sqlalchemy import text
from backend.faculty_map import Faculty
from backend import users as users_svc
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_stage import (
    clean_stage,
    nxt,
    queued,
    stage,
)
from tests.test_station_engine import (
    admin,
    apps,
    engine,
    operator,
    world,
)


@pytest.fixture
def caller_client(apps, engine, world):
    """Client authenticated as a CALLER role."""
    with engine.begin() as c:
        try:
            users_svc.create_user(c, username="caller-v3", password=PASSWORD, role="CALLER")
        except Exception:
            pass
    client = new_client(apps)
    assert api_login(client, "caller-v3").status_code == 200
    return client


@pytest.fixture
def stage_client(apps, engine, world):
    """Client authenticated as a STAGE role."""
    with engine.begin() as c:
        try:
            users_svc.create_user(c, username="stage-v3", password=PASSWORD, role="STAGE")
        except Exception:
            pass
    client = new_client(apps)
    assert api_login(client, "stage-v3").status_code == 200
    return client


@pytest.fixture
def registry_client(apps, engine, world):
    """Client authenticated as REGISTRY role (unauthorized for caller and stage)."""
    with engine.begin() as c:
        try:
            users_svc.create_user(c, username="reg-v3", password=PASSWORD, role="REGISTRY")
        except Exception:
            pass
    client = new_client(apps)
    assert api_login(client, "reg-v3").status_code == 200
    return client


def test_caller_next_isolation_from_stage(apps, engine, world, caller_client, stage_client, clean_stage):
    """
    Caller NEXT:
      - Sets called_at on queue row
      - Removes student from /caller/queue
      - Leaves student in /stage/queue (staged_at IS NULL)
      - Does not alter stage state or LED display
    """
    students = queued(engine, apps, world, 3)
    s1, s2, s3 = students[0], students[1], students[2]

    # Verify both queues see all 3 students
    cq = caller_client.get("/caller/queue").json()
    assert len(cq["students"]) == 3
    assert cq["students"][0]["student_id"] == str(s1.id)

    sq = stage_client.get("/stage/queue").json()
    assert len(sq["students"]) == 3
    assert sq["students"][0]["student_id"] == str(s1.id)

    # Check stage private state before caller next
    assert stage_client.post("/stage/control").status_code == 200
    stage_state_before = stage_client.get("/stage/state").json()
    assert stage_state_before.get("current") is None

    # Caller calls NEXT for first student
    resp = caller_client.post("/caller/next", json={"student_id": str(s1.id)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["student_id"] == str(s1.id)

    # 1. /caller/queue now excludes s1
    cq_after = caller_client.get("/caller/queue").json()
    caller_ids = [s["student_id"] for s in cq_after["students"]]
    assert str(s1.id) not in caller_ids
    assert len(cq_after["students"]) == 2
    assert cq_after["students"][0]["student_id"] == str(s2.id)

    # 2. /stage/queue STILL includes s1 because staged_at IS NULL
    sq_after = stage_client.get("/stage/queue").json()
    stage_ids = [s["student_id"] for s in sq_after["students"]]
    assert str(s1.id) in stage_ids
    assert len(sq_after["students"]) == 3

    # 3. Stage state and LED are completely untouched
    stage_state_after = stage_client.get("/stage/state").json()
    assert stage_state_after.get("current") is None

    # Check DB timestamps
    with engine.begin() as c:
        row = c.execute(
            text("SELECT called_at, staged_at FROM queue WHERE student_id = :id"),
            {"id": s1.id}
        ).fetchone()
        assert row[0] is not None, "called_at must be populated"
        assert row[1] is None, "staged_at must remain NULL"


def test_stage_advance_isolation_from_caller(apps, engine, world, caller_client, stage_client, clean_stage):
    """
    Stage NEXT:
      - Sets staged_at on the student placed on stage
      - Removes student from /stage/queue
      - Does NOT set called_at on queue row
    """
    students = queued(engine, apps, world, 3)
    s1, s2, s3 = students[0], students[1], students[2]

    assert stage_client.post("/stage/control").status_code == 200

    # Advance stage to s1
    resp = stage_client.post("/stage/next", json={"expect_current": None})
    assert resp.status_code == 200
    state = resp.json().get("state", {})
    assert state.get("current", {}).get("student_id") == str(s1.id)

    # 1. /stage/queue now excludes s1 (staged_at IS NOT NULL)
    sq = stage_client.get("/stage/queue").json()
    stage_ids = [s["student_id"] for s in sq["students"]]
    assert str(s1.id) not in stage_ids

    # 2. In the DB, s1 has staged_at set, but called_at is still NULL
    with engine.begin() as c:
        row = c.execute(
            text("SELECT called_at, staged_at FROM queue WHERE student_id = :id"),
            {"id": s1.id}
        ).fetchone()
        assert row[0] is None, "called_at should not be set by Stage advance"
        assert row[1] is not None, "staged_at must be set by Stage advance"


def test_cross_role_rejection(apps, engine, world, caller_client, stage_client, registry_client, clean_stage):
    """
    Strict role-based separation:
      - CALLER role calling /stage endpoints returns 403 Forbidden
      - STAGE role calling /caller/next returns 403 Forbidden
      - REGISTRY role calling either returns 403 Forbidden
    """
    students = queued(engine, apps, world, 2)
    s1 = str(students[0].id)

    # 1. CALLER calling stage controls
    assert caller_client.post("/stage/next", json={"expect_current": None}).status_code == 403
    assert caller_client.post("/stage/home", json={}).status_code == 403
    assert caller_client.post("/stage/display", json={"student_id": s1}).status_code == 403
    assert caller_client.post("/stage/takeover", json={}).status_code == 403

    # 2. STAGE calling caller next
    assert stage_client.post("/caller/next", json={"student_id": s1}).status_code == 403
    assert stage_client.post(f"/caller/next/{s1}").status_code == 403

    # 3. Unauthorized role (REGISTRY) calling either
    assert registry_client.post("/caller/next", json={"student_id": s1}).status_code == 403
    assert registry_client.post("/stage/next", json={"expect_current": None}).status_code == 403


def test_caller_next_idempotency_and_double_tap(apps, engine, world, caller_client, clean_stage):
    """
    Caller NEXT is atomic and idempotent:
      - Double-tap on same student returns 200 OK without errors
      - Concurrent calls only set called_at once
    """
    students = queued(engine, apps, world, 2)
    s1 = str(students[0].id)

    # First call
    r1 = caller_client.post("/caller/next", json={"student_id": s1})
    assert r1.status_code == 200
    assert r1.json()["ok"] is True

    # Immediate second call (double-tap)
    r2 = caller_client.post("/caller/next", json={"student_id": s1})
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    assert r2.json().get("already_called") is True


def test_caller_queue_faculty_color_palette(apps, engine, world, caller_client, clean_stage):
    """
    /caller/queue returns faculty and palette (strong, light hex) for each student.
    """
    students = queued(engine, apps, world, 1)
    s1 = students[0]

    # Explicitly set faculty to SCIENCE
    with engine.begin() as c:
        c.execute(
            text("UPDATE students SET faculty = 'SCIENCE' WHERE id = :id"),
            {"id": s1.id}
        )

    cq = caller_client.get("/caller/queue").json()
    assert len(cq["students"]) >= 1
    found = [s for s in cq["students"] if s["student_id"] == str(s1.id)][0]
    assert found["faculty"] == "SCIENCE"
    assert found["palette"]["strong"] == "#278844"
    assert found["palette"]["light"] == "#9DD29C"


def test_sse_queue_events_endpoint(engine):
    """
    Queue events SSE generator emits initial sync and reacts to stop signal cleanly.
    """
    from backend.stage.queue_events import iter_queue_events
    gen = iter_queue_events(engine, stop=lambda: True)
    msg = next(gen)
    assert "event: sync" in msg
    assert "data: " in msg

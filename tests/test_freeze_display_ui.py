"""Tests for Admin UI Freeze Display Data card and Stage Controller banner.

Requirements:
1. Admin UI: Freeze Display Data card on /admin/system
   - Shows current student count and current frozen count.
   - Confirmation step with warning that names, programmes, schools, awards and photos
     are copied to public LED screen and frozen rows are locked (needs Master Patch).
   - POSTs to /admin/snapshot/freeze. Admin only, audit-logged.
   - After success, shows frozen_count.
2. Stage Controller Banner:
   - If display_snapshot has 0 rows, or the current student has no snapshot row,
     shows a persistent warning explaining the LED will stay on holding screen
     until display data is frozen, with a link to /admin/system#freeze-card.
"""
import uuid
import pytest
from sqlalchemy import text

from backend.admin import reset as reset_svc
from backend.snapshot import freeze_display_data
from backend.stage import state as stage_state
from tests.admin_support import rows, scalar
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_station_engine import (  # noqa: F401
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    q,
    world,
)


def clean_db(engine):
    with engine.begin() as conn:
        reset_svc._lower_guards(conn)
        reset_svc._delete_all(conn)


@pytest.fixture(autouse=True)
def _fresh_client_cache():
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


def get_admin(apps):
    client = new_client(apps)
    assert api_login(client, "eng-admin").status_code == 200
    return client


def get_stage_operator(apps):
    client = new_client(apps)
    assert api_login(client, "eng-stage").status_code == 200
    return client


class TestAdminFreezeDisplayDataCard:
    def test_admin_system_shows_freeze_card_with_counts_and_warnings(self, apps, engine, world):
        clean_db(engine)

        client = get_admin(apps)
        page = client.get("/admin/system").text
        assert 'id="freeze-card"' in page
        assert 'Freeze Display Data for Big Screen' in page
        assert 'data-count="students-total">0</div>' in page
        assert 'data-count="frozen-total">0</div>' in page
        assert 'id="freeze-confirm-check"' in page
        assert 'copied to the public LED screen' in page
        assert 'Master Patch' in page

        # Add 2 students without freezing
        s1 = make_student(engine, name="Freeze Alice")
        s2 = make_student(engine, name="Freeze Bob")

        page = client.get("/admin/system").text
        assert 'data-count="students-total">2</div>' in page
        assert 'data-count="frozen-total">0</div>' in page
        assert 'No display data has been frozen yet' in page

    def test_freeze_endpoint_admin_only_and_audit_logged(self, apps, engine, world):
        clean_db(engine)
        s1 = make_student(engine, name="Freeze Charlie")
        s2 = make_student(engine, name="Freeze Diana")

        # Non-admin rejected
        op = get_stage_operator(apps)
        res = op.post("/admin/snapshot/freeze", headers={"accept": "application/json"})
        assert res.status_code == 403

        # Anonymous rejected
        anon = new_client(apps)
        res = anon.post("/admin/snapshot/freeze", headers={"accept": "application/json"})
        assert res.status_code == 401

        # Admin can freeze via form POST (accept: text/html)
        adm = get_admin(apps)
        res = adm.post("/admin/snapshot/freeze", headers={"accept": "text/html"}, follow_redirects=False)
        assert res.status_code == 303
        assert "/admin/system#freeze-card" in res.headers["location"]

        # Check audit log
        audit_rows = q(engine, "SELECT action, details FROM audit_log WHERE action = 'FREEZE_DISPLAY_SNAPSHOT' ORDER BY id DESC LIMIT 1")
        assert len(audit_rows) == 1
        assert "operator_id" in audit_rows[0]["details"]
        assert audit_rows[0]["details"]["frozen_count"] == 2

        # Follow redirect or GET /admin/system, verify frozen count is shown
        page = adm.get("/admin/system").text
        assert 'data-count="frozen-total">2</div>' in page
        assert 'All 2 student(s) have approved display data' in page


class TestStageControllerBanner:
    def test_stage_page_contains_freeze_warning_banner_and_admin_link(self, apps, engine, world):
        op = get_stage_operator(apps)
        page = op.get("/station/STAGE").text
        assert 'id="freeze-warning"' in page
        assert 'id="freeze-warning-text"' in page
        assert 'href="/admin/system#freeze-card"' in page

    def test_private_state_includes_display_snapshot_count_and_has_display_data(self, apps, engine, world):
        clean_db(engine)

        s = make_student(engine, name="Unfrozen Student")
        # Put in queue
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO queue (student_id, queue_position, status) VALUES (:s, 1, 'QUEUED')"),
                         {"s": s.id})
            stage_state.begin_controller_txn(conn)
            stage_state.update_state(conn, current_student_id=s.id)

        adm = get_admin(apps)
        res = adm.get("/stage/state")
        assert res.status_code == 200
        st = res.json()
        assert "display_snapshot_count" in st
        assert st["display_snapshot_count"] == 0
        assert st["current"] is not None
        assert st["current"]["has_display_data"] is False

        # Now freeze display data
        adm.post("/admin/snapshot/freeze", headers={"accept": "application/json"})

        res = adm.get("/stage/state")
        assert res.status_code == 200
        st = res.json()
        assert st["display_snapshot_count"] >= 1
        assert st["current"]["has_display_data"] is True

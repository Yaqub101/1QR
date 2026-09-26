"""Tests for the Admin UI Freeze Display Data card.

Requirements:
1. Admin UI: Freeze Display Data card on /admin/system
   - Shows current student count and current frozen count.
   - Confirmation step with warning that names, programmes, schools, awards and photos
     are copied into the display snapshot and frozen rows are locked (needs Master Patch).
   - POSTs to /admin/snapshot/freeze. Admin only, audit-logged.
   - After success, shows frozen_count.
"""
import uuid
import pytest
from sqlalchemy import text

from backend.admin import reset as reset_svc
from backend.snapshot import freeze_display_data

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




class TestAdminFreezeDisplayDataCard:
    def test_admin_system_shows_freeze_card_with_counts_and_warnings(self, apps, engine, world):
        clean_db(engine)

        client = get_admin(apps)
        page = client.get("/admin/system").text
        assert 'id="freeze-card"' in page
        assert 'Freeze Display Data</h2>' in page
        assert 'data-count="students-total">0</div>' in page
        assert 'data-count="frozen-total">0</div>' in page
        assert 'id="freeze-confirm-check"' in page
        assert 'copied into the display snapshot' in page
        assert 'LED' not in page
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




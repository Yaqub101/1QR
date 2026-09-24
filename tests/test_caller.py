"""The Caller screen (role/flow redesign, Phase R3, extended with live queue list in Feature: caller queue list).

The existing tests cover the LED-mirror half of the screen: the Caller always shows the same student as the
public LED (driven by the same display_snapshot row), no PRN / phone / email / photo / internal id ever reaches
the stream, and only the Stage operator's NEXT / SEND / HOME / SHOW AGAIN changes either screen.

The new queue list (GET /caller/queue, POST /caller/dismiss) is covered in tests/test_caller_queue.py.
"""
import json
import re

import pytest
from sqlalchemy import text

from backend import users as users_svc
from tests.conftest import run_alembic
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
from tests.test_station_engine import (  # noqa: F401  (engine/world/apps are pytest fixtures)
    admin,
    apps,
    engine,
    operator,
    q,
    world,
)

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
CALLER_KEYS = {"mode", "version", "student"}
CALLER_STUDENT_KEYS = {"name", "programme"}  # THE approved Caller fields


@pytest.fixture(scope="module")
def caller(engine, world, apps):
    with engine.begin() as c:
        users_svc.create_user(c, username="caller-1", password=PASSWORD, role="CALLER")
    client = new_client(apps)
    assert api_login(client, "caller-1").status_code == 200
    return client


def caller_state(client):
    r = client.get("/caller/state")
    assert r.status_code == 200, r.text
    return r.json()


def assert_caller_clean(raw, students):
    data = json.loads(raw)
    assert set(data) == CALLER_KEYS
    if data["student"] is not None:
        assert set(data["student"]) == CALLER_STUDENT_KEYS
    for s in students:
        for secret in (s.prn, str(s.id), s.token):
            assert secret not in raw, f"{secret!r} reached the Caller screen"
    assert not UUID_RE.search(raw), "an id-shaped value reached the Caller screen"
    for banned in ("prn", "phone", "email", "mobile", "photo", "token", "student_id", "school", "award"):
        assert banned not in raw.lower(), banned


def caller_stream(engine, settings, max_ticks=300):
    clock = {"t": 0}

    def monotonic():
        clock["t"] += 1
        return clock["t"]

    from backend.stage import led as led_mod
    return led_mod.iter_caller_events(engine, settings, poll_seconds=0, heartbeat_seconds=2, sleep=lambda s: None,
                                      monotonic=monotonic, stop=lambda: clock["t"] > max_ticks)


def data_of(sse):
    return json.loads(sse.split("data: ", 1)[1])


# =========================================================================== the same student as the LED
class TestAlwaysTheSameStudentAsTheLed:
    def test_every_stage_action_moves_the_caller_and_the_led_together(self, apps, world, engine, stage, caller):
        first, second, third = queued(engine, apps, world, 3)
        claim(stage)

        def both():
            screen, shown = caller_state(caller), led(apps).json()
            assert screen["mode"] == shown["mode"] and screen["version"] == shown["version"]
            if shown["student"] is None:
                assert screen["student"] is None
            else:
                assert screen["student"] == {"name": shown["student"]["name"], "programme": shown["student"]["programme"]}
            return screen["student"] and screen["student"]["name"]

        assert both() is None                          # holding screen before the ceremony
        nxt(stage.main)
        assert both() == first.name
        nxt(stage.main)
        assert both() == second.name                   # NEXT: degree for the first, the second is called
        act(stage.main, "home")
        assert both() is None                          # emergency HOME: nobody to call
        act(stage.main, "show-again")
        assert both() == second.name
        act(stage.main, "display", student_id=str(third.id), expect_current=str(second.id))
        assert both() == third.name                    # SEND from the waiting list
        nxt(stage.main)
        assert both() is None                          # the last one: back to holding on both

    def test_the_caller_reads_the_approved_display_snapshot_not_the_student_record(self, apps, world, engine, stage, caller):
        s = queued(engine, apps, world, 1, snapshot=False)[0]
        with engine.begin() as c:  # the approved display name differs from the raw master-list name
            c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school) VALUES "
                           "(:s, 'Dr. Approved Display Name', 'Doctor of Philosophy (Physics)', 'School of Science')"),
                      {"s": s.id})
        claim(stage)
        nxt(stage.main)
        assert caller_state(caller)["student"] == {"name": "Dr. Approved Display Name",
                                                   "programme": "Doctor of Philosophy (Physics)"}
        assert led(apps).json()["student"]["name"] == "Dr. Approved Display Name"

    def test_a_student_with_no_approved_display_data_is_on_neither_screen(self, apps, world, engine, stage, caller):
        queued(engine, apps, world, 1, snapshot=False)
        claim(stage)
        nxt(stage.main)
        assert caller_state(caller)["mode"] == "HOME" and caller_state(caller)["student"] is None
        assert led(apps).json()["mode"] == "HOME"

    def test_a_queue_scan_never_changes_the_caller(self, apps, world, engine, stage, caller):
        first = queued(engine, apps, world, 1)[0]
        claim(stage)
        nxt(stage.main)
        before = caller_state(caller)
        queued(engine, apps, world, 3)
        assert caller_state(caller) == before and before["student"]["name"] == first.name

    def test_the_caller_and_led_streams_announce_each_change_on_the_same_poll(self, apps, world, engine, stage):
        first, second = queued(engine, apps, world, 2)
        claim(stage)
        calls, leds = caller_stream(engine, apps.state.settings), bounded_stream(engine, apps.state.settings)
        assert data_of(next(calls))["mode"] == data_of(next(leds))["mode"] == "HOME"
        nxt(stage.main)
        c, l = data_of(next(calls)), data_of(next(leds))
        assert c["version"] == l["version"] and c["student"]["name"] == l["student"]["name"] == first.name
        queued(engine, apps, world, 1)                                  # a queue scan...
        assert next(calls).startswith("event: ping")                    # ...reaches neither stream
        assert next(leds).startswith("event: ping")
        nxt(stage.main)
        c, l = data_of(next(calls)), data_of(next(leds))
        assert c["version"] == l["version"] and c["student"]["name"] == l["student"]["name"] == second.name


# =========================================================================== nothing but name + programme
class TestOnlyApprovedFields:
    def test_the_state_and_the_stream_carry_only_name_and_programme(self, apps, world, engine, stage, caller):
        students = queued(engine, apps, world, 3)
        claim(stage)
        nxt(stage.main)
        response = caller.get("/caller/state")
        assert response.headers["cache-control"] == "no-store"
        assert_caller_clean(response.text, students)
        assert_caller_clean(next(caller_stream(engine, apps.state.settings)).split("data: ", 1)[1], students)

    def test_the_page_has_no_static_controls_and_no_student_data_baked_in(self, apps, world, engine, stage, caller):
        students = queued(engine, apps, world, 2)
        claim(stage)
        nxt(stage.main)
        page = caller.get("/caller")
        assert page.status_code == 200
        # No static HTML controls (the Complete buttons are injected by JS, not server-rendered).
        for control in ("<form", "<input", "<select", "<textarea"):
            assert control not in page.text, control
        for secret in (students[0].prn, str(students[0].id), students[0].name):
            assert secret not in page.text  # everything arrives through the stream, never rendered into the page
        # Queue IDs from the caller template
        assert 'id="cq-list"' in page.text and 'id="cq-count"' in page.text
        assert "/static/caller.js" in page.text and "http://" not in page.text and "https://" not in page.text
        assert new_client(apps).get("/static/caller.js").status_code == 200

    def test_the_stream_route_is_server_sent_events(self, apps):
        from backend.stage import routes as stage_routes
        response = stage_routes.caller_events_response(apps)
        assert response.media_type == "text/event-stream" and response.headers["cache-control"] == "no-store"


# =========================================================================== who may see it, and that it controls nothing
class TestAccess:
    @pytest.mark.parametrize("path", ["/caller", "/caller/state"])
    def test_the_caller_the_stage_operator_and_the_admin_may_open_it(self, apps, world, stage, caller, path):
        for client in (caller, stage.main, admin(apps)):
            assert client.get(path).status_code == 200

    @pytest.mark.parametrize("activity", ["REGISTRATION", "SEATING", "QUEUE", "LUNCH"])
    def test_other_operators_may_not(self, apps, world, activity):
        client = operator(apps, world, activity)
        for path in ("/caller", "/caller/state", "/caller/events"):
            response = client.get(path)
            assert response.status_code == 403 and response.json()["detail"]["message"] == "That screen is not part of your role."

    def test_nobody_signed_out_may(self, apps):
        for path in ("/caller", "/caller/state", "/caller/events"):
            assert new_client(apps).get(path).status_code == 401

    def test_the_caller_cannot_touch_the_stage_or_record_anything(self, apps, world, engine, stage, caller):
        s = queued(engine, apps, world, 1)[0]
        claim(stage)
        nxt(stage.main)
        before = q(engine, "SELECT md5(t::text) AS f FROM stage_state t")[0]["f"]
        for path, body in [("next", {"expect_current": str(s.id)}), ("show-again", {}), ("home", {}), ("previous", {}),
                           ("skip", {"reason": "x"}), ("display", {"student_id": str(s.id), "expect_current": None}),
                           ("search", {"q": "x"}), ("control", {}), ("takeover", {})]:
            assert act(caller, path, station="CALLER", **body).status_code == 403, path
        assert caller.get("/stage/state").status_code == 403
        for activity in ("REGISTRY", "QUEUE", "LUNCH", "SEATING"):
            assert caller.post("/scan", json={"activity": activity, "token": s.token}).status_code == 403, activity
        assert caller.get("/admin").status_code == 403
        assert q(engine, "SELECT md5(t::text) AS f FROM stage_state t")[0]["f"] == before

    def test_the_admin_can_create_a_caller_account(self, apps, world, engine):
        page = admin(apps).get("/admin/users").text
        assert '<option value="CALLER">' in page


# =========================================================================== the migration
class TestCallerRoleMigration:
    def test_the_database_accepts_caller_only_after_the_migration(self, bare_database):
        from sqlalchemy import create_engine

        assert run_alembic("upgrade", "0014_registry_role", database_url=bare_database).returncode == 0
        eng = create_engine(bare_database)
        try:
            with eng.connect() as c:
                with pytest.raises(Exception):
                    with c.begin():
                        c.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('c0', 'h', 'CALLER')"))
            result = run_alembic("upgrade", "head", database_url=bare_database)
            assert result.returncode == 0, result.stdout + result.stderr
            with eng.begin() as c:
                c.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('c1', 'h', 'CALLER')"))
            refused = run_alembic("downgrade", "0014_registry_role", database_url=bare_database)
            assert refused.returncode != 0 and "CALLER" in (refused.stdout + refused.stderr)  # never silently drops accounts
            with eng.connect() as c:
                assert c.execute(text("SELECT role FROM users WHERE username = 'c1'")).scalar_one() == "CALLER"
        finally:
            eng.dispose()

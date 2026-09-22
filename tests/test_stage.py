"""Phase 11 - Stage Controller and public LED.

Focused on what would be visibly wrong to an audience or an operator: the LED shows the right student,
HOME is instant, COMPLETE/SKIP record the right thing exactly once, a double press never advances twice,
only ONE laptop controls the stage (and "take over" really locks the old one out), and the public LED
payload contains nothing but approved fields.

Runs against ONE shared PostgreSQL test database with three venue app instances (as in the other engine
suites). The 10-second hold is the LED page's own logic: tests/js/led.test.js drives it with a mocked clock.
"""
import hashlib
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend import stations as stations_svc
from backend import users as users_svc
from backend.engine import service
from backend.snapshot import begin_master_patch_txn
from backend.stage import led as led_mod
from tests.test_auth import OWNER, PASSWORD, api_login, new_client
from tests.test_schema import RESTRICT_VIOLATION, _run_threads, db_error
from tests.test_station_engine import (  # noqa: F401  (engine/world/apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    events_of,
    log_of,
    make_student,
    operator,
    q,
    ready_student,
    scan,
    world,
)

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
LED_STUDENT_KEYS = {"name", "photo_url", "programme", "school", "award"}  # THE approved LED payload


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module")
def stage(engine, world, apps):
    """Two Stage laptops: STG-01 (main) and STG-02 (backup), each with its own operator and session."""
    with engine.begin() as c:
        users_svc.create_user(c, username="stage-backup", password=PASSWORD, role="STAGE")
        stations_svc.create_station(c, venue_id="stadium", station_id="STG-02", activity="STAGE")
        device = stations_svc.bind_station(c, "STG-02", actor_id=world.admin_id)
    main = operator(apps, world, "STAGE")
    backup = new_client(apps["stadium"], device)
    assert api_login(backup, "stage-backup").status_code == 200
    return SimpleNamespace(main=main, backup=backup, main_token=main.cookies.get("session"),
                           backup_token=backup.cookies.get("session"))


def reset_stage(engine):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.stage_controller', 'on', true)"))
        c.execute(text("UPDATE stage_state SET current_student_id = NULL, display_student_id = NULL, previous_student_id = NULL, "
                       "controller_session_id = NULL, controller_station_id = NULL, controller_since = NULL WHERE id = 1"))
        c.execute(text("UPDATE queue SET status = 'DONE' WHERE status IN ('QUEUED','DISPLAYED','HELD','SKIPPED')"))


@pytest.fixture(autouse=True)
def clean_stage(engine, stage):
    reset_stage(engine)
    yield


def add_snapshot(engine, s, award="Gold medal"):
    with engine.begin() as c:
        c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school, award) "
                       "VALUES (:s, :n, 'B.Tech Computer Science', 'School of Engineering', :a) ON CONFLICT DO NOTHING"),
                  {"s": s.id, "n": s.name, "a": award})


def queued(engine, apps, world, n, *, snapshot=True):
    """n students who really went through the Queue engine, in confirmation order."""
    out = []
    for _ in range(n):
        s = ready_student(engine, "QUEUE", name=f"Graduate {uuid.uuid4().hex[:8]}")
        if snapshot:
            add_snapshot(engine, s)
        assert confirm(operator(apps, world, "QUEUE"), "QUEUE", token=s.token).json()["result"] == "CONFIRMED"
        out.append(s)
    return out


def act(client, path, station="STG-01", **body):
    return client.post(f"/stage/{path}", json={"station_id": station, **body})


def claim(stage):
    r = act(stage.main, "control")
    assert r.status_code == 200, r.text
    return r


def led(apps):
    return new_client(apps["stadium"]).get("/led/state")


def stage_stat(engine, student):
    return q(engine, "SELECT status FROM queue WHERE student_id = :s", s=student.id)[0]["status"]


def state_fingerprint(engine):
    return q(engine, "SELECT md5(t::text) AS f FROM stage_state t")[0]["f"]


def snapshot_fingerprint(engine):
    return q(engine, "SELECT md5(coalesce(string_agg(d::text || d.xmin::text, '|' ORDER BY d.student_id), '')) AS f FROM display_snapshot d")[0]["f"]


# =========================================================================== DISPLAY NEXT
class TestDisplayNext:
    def test_shows_the_first_queued_student_on_the_led_quickly(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 3)
        claim(stage)
        snap_before = snapshot_fingerprint(engine)
        response = act(stage.main, "display-next")
        assert response.status_code == 200 and float(response.headers["x-process-time-ms"]) < 200
        body = led(apps).json()
        assert body["mode"] == "SHOWING" and body["student"]["name"] == students[0].name  # first come, first shown
        assert body["student"]["programme"] == "B.Tech Computer Science" and body["student"]["award"] == "Gold medal"
        assert [stage_stat(engine, s) for s in students] == ["DISPLAYED", "QUEUED", "QUEUED"]
        state = response.json()["state"]
        assert (state["current"]["name"], state["next"]["name"], state["after_next"]["name"]) == \
               (students[0].name, students[1].name, students[2].name)
        assert snapshot_fingerprint(engine) == snap_before  # the LED READS the approved snapshot; nothing rewrites it

    def test_the_led_follows_first_come_first_shown_not_the_university_sequence(self, apps, world, engine, stage):
        first, second = ready_student(engine, "QUEUE"), ready_student(engine, "QUEUE")
        for s in (first, second):
            add_snapshot(engine, s)
        for s in (second, first):  # the higher sequence number confirms its queue FIRST
            confirm(operator(apps, world, "QUEUE"), "QUEUE", token=s.token)
        claim(stage)
        act(stage.main, "display-next")
        assert led(apps).json()["student"]["name"] == second.name

    def test_no_other_endpoint_can_change_it_a_queue_station_is_rejected(self, apps, world, engine, stage):
        s = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        state_before, snap_before = state_fingerprint(engine), snapshot_fingerprint(engine)
        queue_client = operator(apps, world, "QUEUE")
        attempts = [("display-next", {}), ("home", {}), ("previous", {}), ("complete", {}), ("skip", {"reason": "x"}),
                    ("display", {"student_id": str(s[1].id)}), ("search", {"q": "x"}), ("control", {}), ("takeover", {})]
        for path, body in attempts:
            response = queue_client.post(f"/stage/{path}", json={"station_id": "QUE-01", **body})
            assert response.status_code == 403, (path, response.status_code)
        # ...and ordinary Queue work (scan / confirm) leaves the LED state and snapshot untouched too
        extra = ready_student(engine, "QUEUE")
        scan(queue_client, "QUEUE", extra.token)
        confirm(queue_client, "QUEUE", token=extra.token)
        assert state_fingerprint(engine) == state_before and snapshot_fingerprint(engine) == snap_before
        assert led(apps).json()["student"]["name"] == s[0].name

    def test_the_database_itself_refuses_any_change_that_is_not_the_stage_controller(self, engine):
        with engine.connect() as conn:
            outer = conn.begin()
            try:
                with db_error(conn, RESTRICT_VIOLATION, match="Stage Controller"):
                    conn.execute(text("UPDATE stage_state SET display_student_id = current_student_id WHERE id = 1"))
                with db_error(conn, RESTRICT_VIOLATION):
                    conn.execute(text("DELETE FROM stage_state"))
            finally:
                outer.rollback()

    def test_an_empty_queue_says_so_plainly_and_changes_nothing(self, apps, world, engine, stage):
        claim(stage)
        before = state_fingerprint(engine)
        response = act(stage.main, "display-next")
        assert response.status_code == 409 and response.json()["detail"]["message"] == "Nobody is waiting in the queue."
        assert state_fingerprint(engine) == before

    def test_a_student_with_no_approved_display_data_never_reaches_the_led(self, apps, world, engine, stage):
        # ASSUMPTION (flagged): the operator still gets the student on the private screen, the LED stays on
        # the holding screen rather than showing unapproved data, and the ceremony is not blocked.
        s = queued(engine, apps, world, 1, snapshot=False)[0]
        claim(stage)
        response = act(stage.main, "display-next")
        assert response.status_code == 200 and response.json()["state"]["current"]["name"] == s.name
        assert response.json()["state"]["current"]["has_display_data"] is False
        assert led(apps).json()["mode"] == "HOME" and led(apps).json()["student"] is None


# =========================================================================== HOME / PREVIOUS / SEARCH
class TestHomePreviousSearch:
    def test_home_reverts_the_led_to_the_holding_screen_at_once(self, apps, world, engine, stage):
        queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        assert led(apps).json()["mode"] == "SHOWING"
        response = act(stage.main, "home")
        assert response.status_code == 200 and float(response.headers["x-process-time-ms"]) < 200
        body = led(apps).json()
        assert body["mode"] == "HOME" and body["student"] is None and body["holding"]["title"]
        assert response.json()["state"]["current"] is not None  # the student is still on stage, just not on screen

    def test_pressing_display_next_after_home_reshows_the_same_student_it_never_skips_ahead(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        act(stage.main, "home")
        act(stage.main, "display-next")
        assert led(apps).json()["student"]["name"] == students[0].name
        assert stage_stat(engine, students[1]) == "QUEUED"

    def test_previous_returns_the_wrong_student_to_the_front_of_the_queue(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        r = act(stage.main, "previous")
        assert r.status_code == 200 and led(apps).json()["mode"] == "HOME"
        assert r.json()["state"]["current"] is None and stage_stat(engine, students[0]) == "QUEUED"
        act(stage.main, "display-next")
        assert led(apps).json()["student"]["name"] == students[0].name  # still first in line
        assert events_of(engine, students[0], "STAGE") == []            # PREVIOUS records no Stage event

    def test_search_finds_a_queued_student_and_display_shows_them_out_of_order_with_an_audit_trail(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 3)
        claim(stage)
        found = act(stage.main, "search", q=students[2].prn.lower()).json()["matches"]
        assert [m["name"] for m in found] == [students[2].name] and found[0]["queue_position"] > 0
        assert act(stage.main, "display", student_id=found[0]["student_id"]).status_code == 200
        assert led(apps).json()["student"]["name"] == students[2].name
        actions = {r["action"] for r in q(engine, "SELECT action FROM audit_log WHERE action LIKE 'STAGE_%'")}
        assert {"STAGE_SEARCH", "STAGE_DISPLAY"} <= actions  # append-only trail (audit_log rejects UPDATE/DELETE)


# =========================================================================== COMPLETE / SKIP
class TestCompleteAndSkip:
    def test_complete_creates_the_stage_event_and_a_queue_only_student_has_none(self, apps, world, engine, stage):
        first, waiting = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        assert events_of(engine, first, "STAGE") == [] and events_of(engine, waiting, "STAGE") == []  # displayed is not completed
        response = act(stage.main, "complete")
        assert response.status_code == 200
        event = events_of(engine, first, "STAGE")
        assert len(event) == 1 and event[0]["kind"] == "COMPLETE" and event[0]["station_id"] == "STG-01"
        assert "MANUAL" not in event[0]["flags"]  # the controller identified them; nobody typed a PRN
        assert events_of(engine, waiting, "STAGE") == []  # only reached the Queue: no Stage record
        assert q(engine, "SELECT count(*) AS n FROM outbox WHERE event_id = :e", e=event[0]["event_id"])[0]["n"] == 1
        assert stage_stat(engine, first) == "DONE" and stage_stat(engine, waiting) == "QUEUED"
        assert q(engine, "SELECT status FROM student_status WHERE student_id = :s", s=first.id)[0]["status"] == "THOBE NOT RETURNED"
        assert led(apps).json()["mode"] == "HOME"  # a holding screen between students
        assert response.json()["state"]["current"] is None and response.json()["state"]["previous"]["name"] == first.name

    def test_complete_with_nobody_on_stage_is_refused(self, apps, world, engine, stage):
        s = queued(engine, apps, world, 1)[0]
        claim(stage)
        response = act(stage.main, "complete")
        assert response.status_code == 409 and response.json()["detail"]["message"] == "Nobody is on stage."
        assert events_of(engine, s, "STAGE") == []

    def test_a_failure_during_complete_leaves_the_stage_exactly_as_it_was(self, apps, world, engine, stage, monkeypatch):
        s = queued(engine, apps, world, 1)[0]
        claim(stage)
        act(stage.main, "display-next")
        before = state_fingerprint(engine)

        def boom(*a, **k):
            raise RuntimeError("simulated crash after the event insert")

        monkeypatch.setattr(service, "insert_outbox", boom)
        response = act(stage.main, "complete")
        assert response.status_code == 503 and response.json()["detail"]["message"] == "One moment, please try again."
        monkeypatch.undo()
        assert events_of(engine, s, "STAGE") == [] and state_fingerprint(engine) == before and stage_stat(engine, s) == "DISPLAYED"
        assert act(stage.main, "complete").status_code == 200  # the retry works

    @pytest.mark.parametrize("reason", [None, "", "   ", "\n\t"])
    def test_skip_without_a_reason_is_rejected(self, apps, world, engine, stage, reason):
        s = queued(engine, apps, world, 1)[0]
        claim(stage)
        act(stage.main, "display-next")
        before = state_fingerprint(engine)
        body = {} if reason is None else {"reason": reason}
        response = act(stage.main, "skip", **body)
        assert response.status_code in (400, 422)
        assert events_of(engine, s, "STAGE") == [] and state_fingerprint(engine) == before and stage_stat(engine, s) == "DISPLAYED"

    def test_skip_with_a_reason_records_it_and_the_student_can_still_be_completed_later(self, apps, world, engine, stage):
        first, second = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        assert act(stage.main, "skip", reason="Not present at the stage").status_code == 200
        skip = events_of(engine, first)
        skip = [e for e in skip if e["activity"] == "STAGE"]
        assert [(e["kind"], e["details"]["reason"]) for e in skip] == [("SKIP", "Not present at the stage")]
        assert stage_stat(engine, first) == "SKIPPED" and led(apps).json()["mode"] == "HOME"
        act(stage.main, "display-next")
        assert led(apps).json()["student"]["name"] == second.name  # the queue moved on
        act(stage.main, "complete")
        found = act(stage.main, "search", q=first.prn).json()["matches"]  # the skipped student is found again
        assert act(stage.main, "display", student_id=found[0]["student_id"]).status_code == 200
        assert act(stage.main, "complete").status_code == 200
        assert [e["kind"] for e in events_of(engine, first) if e["activity"] == "STAGE"] == ["SKIP", "COMPLETE"]


# =========================================================================== DOUBLE PRESS
class TestRapidDoublePress:
    def test_a_rapid_double_press_of_display_next_never_advances_twice(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 4)
        claim(stage)
        results = _run_threads(lambda i: stage_call(apps, stage.main_token, "display-next").status_code, 8)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert set(results) == {200}
        assert [stage_stat(engine, s) for s in students] == ["DISPLAYED", "QUEUED", "QUEUED", "QUEUED"]
        assert led(apps).json()["student"]["name"] == students[0].name

    def test_a_rapid_double_press_of_complete_records_exactly_one_stage_event(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 3)
        claim(stage)
        act(stage.main, "display-next")
        results = _run_threads(lambda i: stage_call(apps, stage.main_token, "complete").status_code, 8)
        assert sorted(results).count(200) == 1 and set(results) <= {200, 409}, results
        assert len(events_of(engine, students[0], "STAGE")) == 1
        assert all(events_of(engine, s, "STAGE") == [] for s in students[1:])  # it did not run ahead
        assert [stage_stat(engine, s) for s in students] == ["DONE", "QUEUED", "QUEUED"]

    def test_a_double_press_of_skip_records_one_skip(self, apps, world, engine, stage):
        s = queued(engine, apps, world, 2)[0]
        claim(stage)
        act(stage.main, "display-next")
        results = _run_threads(lambda i: stage_call(apps, stage.main_token, "skip", reason="late").status_code, 6)
        assert sorted(results).count(200) == 1
        assert len([e for e in events_of(engine, s) if e["activity"] == "STAGE" and e["kind"] == "SKIP"]) == 1


def stage_call(apps, token, path, station="STG-01", **body):
    client = new_client(apps["stadium"])
    client.headers["Authorization"] = f"Bearer {token}"
    return client.post(f"/stage/{path}", json={"station_id": station, **body})


# =========================================================================== ONE CONTROLLER
class TestSingleController:
    def test_only_one_laptop_controls_the_stage_and_take_over_locks_the_old_one_out(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 3)
        assert act(stage.main, "control").status_code == 200                       # laptop A takes control
        assert act(stage.main, "display-next").status_code == 200

        # B cannot act, and cannot quietly claim while A is alive
        refused = act(stage.backup, "display-next", station="STG-02")
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "NOT_CONTROLLER"
        claim_b = act(stage.backup, "control", station="STG-02")
        assert claim_b.status_code == 409 and claim_b.json()["detail"]["code"] == "CONTROLLED_ELSEWHERE"
        assert q(engine, "SELECT count(*) AS n FROM stage_state")[0]["n"] == 1     # one controller slot, by construction

        # B takes over on purpose
        took = act(stage.backup, "takeover", station="STG-02")
        assert took.status_code == 200 and took.json()["state"]["you_control"] is True

        # A is now locked out of EVERY action, and its screen is told so
        for path, body in [("display-next", {}), ("home", {}), ("previous", {}), ("complete", {}),
                           ("skip", {"reason": "x"}), ("display", {"student_id": str(students[1].id)}), ("search", {"q": "x"})]:
            r = act(stage.main, path, **body)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_CONTROLLER", (path, r.status_code, r.text)
        mine = stage.main.get("/stage/state", params={"station_id": "STG-01"}).json()
        assert mine["you_control"] is False and mine["controller"]["station_id"] == "STG-02"
        assert events_of(engine, students[0], "STAGE") == []                       # A's locked-out COMPLETE did nothing

        # B now runs the show; A can take it back deliberately
        assert act(stage.backup, "complete", station="STG-02").status_code == 200
        assert act(stage.backup, "display-next", station="STG-02").status_code == 200
        assert led(apps).json()["student"]["name"] == students[1].name
        assert act(stage.main, "takeover").status_code == 200
        assert act(stage.backup, "home", station="STG-02").status_code == 409

    def test_take_over_is_audited_with_who_replaced_whom(self, apps, world, engine, stage):
        claim(stage)
        act(stage.backup, "takeover", station="STG-02")
        row = q(engine, "SELECT operator_id, station_id, details FROM audit_log WHERE action = 'STAGE_TAKEOVER' ORDER BY id DESC LIMIT 1")[0]
        assert row["station_id"] == "STG-02" and row["details"]["replaced_station"] == "STG-01"

    def test_a_dead_controller_does_not_block_the_backup(self, apps, world, engine, stage):
        claim(stage)
        with engine.begin() as c:  # laptop A's session ended (idle timeout / logged out)
            c.execute(text("UPDATE sessions SET revoked_at = now() WHERE token_hash = :h"),
                      {"h": hashlib.sha256(stage.main_token.encode()).hexdigest()})
        assert act(stage.backup, "control", station="STG-02").status_code == 200  # no take-over needed
        with engine.begin() as c:  # put A's session back for the rest of the module
            c.execute(text("UPDATE sessions SET revoked_at = NULL WHERE token_hash = :h"),
                      {"h": hashlib.sha256(stage.main_token.encode()).hexdigest()})

    def test_an_admin_can_take_over_but_a_queue_operator_cannot(self, apps, world, engine, stage):
        claim(stage)
        assert act(operator(apps, world, "QUEUE"), "takeover", station="QUE-01").status_code == 403
        assert act(admin(apps, "stadium"), "takeover", station="STG-01").status_code == 200
        assert act(stage.main, "home").status_code == 409

    def test_the_stage_screen_is_stadium_only(self, apps, world):
        for venue in ("college", "hall", "central"):
            assert admin(apps, venue).post("/stage/control", json={"station_id": "STG-01"}).status_code == 403



def bounded_stream(engine, settings, *, max_ticks=300):
    """The LED event stream on a fake clock that ENDS after max_ticks polls. A broken stream then fails the
    test with StopIteration instead of hanging it forever."""
    clock = {"t": 0}

    def monotonic():
        clock["t"] += 1
        return clock["t"]

    return led_mod.iter_led_events(engine, settings, poll_seconds=0, heartbeat_seconds=2, sleep=lambda s: None,
                                   monotonic=monotonic, stop=lambda: clock["t"] > max_ticks)


# =========================================================================== THE PUBLIC LED
def assert_led_clean(raw, student):
    """The bytes actually sent to the audience screen: approved fields only."""
    data = json.loads(raw)
    assert set(data) == {"mode", "version", "holding", "student", "preload"}
    assert set(data["holding"]) == {"title", "text"}
    if data["student"] is not None:
        assert set(data["student"]) == LED_STUDENT_KEYS
    for entry in data["preload"]:
        assert set(entry) == {"photo_url"}  # preloaded photos carry no name or id
    for secret in (student.prn, str(student.id), str(student.seq), student.token):
        assert secret not in raw, f"{secret!r} leaked to the LED"
    assert not UUID_RE.search(raw), "an id-shaped value leaked to the LED"
    banned = ("prn", "phone", "email", "mobile", "sequence", "seat", "queue", "token", "student_id", "session", "operator", "controller")
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert not any(b in k.lower() for b in banned) and k.lower() != "id" and not k.lower().endswith("_id"), k
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)


class TestPublicLed:
    def test_the_serialized_led_response_contains_only_approved_fields(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 6)
        claim(stage)
        act(stage.main, "display-next")
        anon = new_client(apps["stadium"])
        response = anon.get("/led/state")
        assert response.status_code == 200
        assert_led_clean(response.text, students[0])
        assert response.json()["student"]["photo_url"].startswith("/led/photo/")
        assert len(response.json()["preload"]) in (3, 4, 5)  # the next few photos, ready before they are needed
        for s in students:
            assert_led_clean(response.text, s)
        act(stage.main, "home")
        assert_led_clean(anon.get("/led/state").text, students[0])

    def test_the_page_and_the_event_stream_carry_the_same_clean_payload(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 2)
        claim(stage)
        act(stage.main, "display-next")
        page = new_client(apps["stadium"]).get("/led")
        assert page.status_code == 200 and "<form" not in page.text and "<button" not in page.text and "<input" not in page.text
        for secret in (students[0].prn, str(students[0].id)):
            assert secret not in page.text
        events = bounded_stream(engine, apps["stadium"].state.settings)
        first = next(events)
        assert first.startswith("event: state\ndata: ") and first.endswith("\n\n")
        assert_led_clean(first.split("data: ", 1)[1], students[0])

    def test_the_led_photo_url_is_opaque_and_serves_the_approved_photo_without_a_login(self, apps, world, engine, stage, tmp_path):
        picture = tmp_path / "gold.jpg"
        picture.write_bytes(b"\xff\xd8\xff\xe0-approved-photo")
        s = queued(engine, apps, world, 1)[0]
        with engine.begin() as c:
            # Frozen display data only moves through the sanctioned door (migration 0010); a plain
            # UPDATE here would be refused by the database, which is the point of the guard.
            begin_master_patch_txn(c)
            c.execute(text("UPDATE display_snapshot SET photo_path = :p WHERE student_id = :s"), {"p": str(picture), "s": s.id})
        claim(stage)
        act(stage.main, "display-next")
        anon = new_client(apps["stadium"])
        url = anon.get("/led/state").json()["student"]["photo_url"]
        assert str(s.id) not in url and s.prn not in url
        photo = anon.get(url)
        assert photo.status_code == 200 and photo.content == picture.read_bytes()
        assert anon.get(f"/led/photo/{s.id}").status_code == 404 and anon.get("/led/photo/nonsense").status_code == 404

    def test_a_queue_scan_or_confirmation_never_changes_what_the_led_shows(self, apps, world, engine, stage):
        first = queued(engine, apps, world, 1)[0]
        claim(stage)
        act(stage.main, "display-next")
        before = led(apps).json()
        queued(engine, apps, world, 3)  # three more students confirm at the Queue
        after = led(apps).json()
        assert after["student"] == before["student"] and after["mode"] == "SHOWING"
        assert after["student"]["name"] == first.name

    def test_the_holding_screen_carries_the_event_branding(self, apps, world, engine, stage):
        with engine.begin() as c:
            c.execute(text("UPDATE settings SET event_name = 'Annual Convocation 2026', holding_screen_text = 'Welcome, graduates'"))
        body = led(apps).json()
        assert body["mode"] == "HOME" and body["holding"] == {"title": "Annual Convocation 2026", "text": "Welcome, graduates"}

    def test_the_led_exists_only_at_the_stadium(self, apps):
        for venue in ("college", "hall", "central"):
            client = new_client(apps[venue])
            assert client.get("/led").status_code == 404 and client.get("/led/state").status_code == 404

    def test_the_event_stream_announces_every_change_and_keeps_the_connection_alive(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 2)
        claim(stage)
        events = bounded_stream(engine, apps["stadium"].state.settings)
        assert json.loads(next(events).split("data: ", 1)[1])["mode"] == "HOME"       # initial paint
        act(stage.main, "display-next")
        shown = next(events)                                                           # the very next poll sees it
        assert shown.startswith("event: state") and json.loads(shown.split("data: ", 1)[1])["student"]["name"] == students[0].name
        assert next(events).startswith("event: ping")                                  # quiet: a heartbeat, so the page can tell it is connected
        act(stage.main, "home")
        assert json.loads(next(events).split("data: ", 1)[1])["mode"] == "HOME"

    def test_the_stream_route_is_server_sent_events(self, apps):
        from backend.stage import routes as stage_routes
        response = stage_routes.led_events_response(apps["stadium"])
        assert response.media_type == "text/event-stream" and response.headers["cache-control"] == "no-store"


# =========================================================================== THE OPERATOR SCREEN
class TestStageScreen:
    def test_the_stage_screen_has_every_control_and_no_external_resources(self, apps, world, stage):
        page = stage.main.get("/station/stage")
        assert page.status_code == 200
        for label in ("DISPLAY NEXT", "HOME", "PREVIOUS", "SEARCH", "SKIP", "COMPLETE", "TAKE OVER"):
            assert label in page.text, label
        for region in ("CURRENT", "NEXT", "AFTER NEXT"):
            assert region in page.text
        assert "/static/stage.js" in page.text and "http://" not in page.text and "https://" not in page.text

    def test_the_static_files_are_served_locally(self, apps, world, stage):
        for path in ("/static/stage.js", "/static/led.js", "/static/led.css"):
            assert stage.main.get(path).status_code == 200
        assert new_client(apps["stadium"]).get("/static/led.js").status_code == 200  # the LED page needs it without a login

    def test_the_private_state_needs_a_stage_role_and_shows_the_three_positions(self, apps, world, engine, stage):
        students = queued(engine, apps, world, 3)
        claim(stage)
        act(stage.main, "display-next")
        state = stage.main.get("/stage/state", params={"station_id": "STG-01"}).json()
        assert state["you_control"] is True and state["queue_depth"] == 2
        assert all(k in state["current"] for k in ("name", "photo_url", "programme", "school", "has_display_data"))
        assert new_client(apps["stadium"]).get("/stage/state").status_code == 401
        assert operator(apps, world, "QUEUE").get("/stage/state", params={"station_id": "QUE-01"}).status_code == 403

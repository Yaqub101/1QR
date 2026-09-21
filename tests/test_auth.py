"""Phase 5 - auth, roles, stations, venue ownership.

Golden rules under test (AGENTS.md):
  2. The station decides the activity. Operators never choose it.
  4. Each activity has exactly one owning venue. Reject other writes.
  11. Operator messages are one plain sentence.

SYSTEM_SPEC section 4 (roles) and 11.2 (single writer) are the source of truth.
The expected-access table below is written out by hand from section 4 and does
NOT import backend.security.permissions, so the implementation is checked
against the spec, not against itself.

Session policy assumed for a multi-hour event day (documented in the report):
  * idle timeout   120 minutes, sliding (any request renews it)
  * absolute limit  12 hours  (covers a full day; forces a fresh login next day)
"""
import hashlib
import re
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend import stations as stations_svc
from backend import users as users_svc
from backend.config import Settings
from backend.main import create_app
from backend.security import ownership, passwords, permissions
from backend.security.deps import require_can_originate, require_user
from backend.seed import SeedError, seed_admins
from tests.conftest import TEST_DB_URL
from tests.test_schema import (
    CHECK_VIOLATION,
    REPO_ROOT,
    RESTRICT_VIOLATION,
    UNIQUE_VIOLATION,
    db_error,
    drop_everything,
    run_alembic,
)

PASSWORD = "Test-Pass-2026!"  # test-only constant, never a real credential
SESSION_COOKIE = "session"
DEVICE_COOKIE = "station_device"
COOKIE_DOMAIN = "testserver.local"  # what http.cookiejar uses for host "testserver"

ACTIVITIES = [
    "REGISTRATION", "THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE", "THOBE_RETURN", "LUNCH",
]
OWNER = {
    "REGISTRATION": "college",
    "THOBE_ALLOCATION": "stadium", "SEATING": "stadium", "QUEUE": "stadium", "STAGE": "stadium",
    "THOBE_RETURN": "hall", "LUNCH": "hall",
}
VENUES = ["college", "stadium", "hall"]

# key -> role. Section 4: seven operator roles + Central Event Admin + the named deputy.
# (That is 9 identities; the request said "8 roles" - see report.)
IDENTITIES = {
    "admin": "ADMIN",
    "deputy": "DEPUTY_ADMIN",
    "registration": "REGISTRATION",
    "thobe_allocation": "THOBE_ALLOCATION",
    "seating": "SEATING",
    "queue": "QUEUE",
    "stage": "STAGE",
    "thobe_return": "THOBE_RETURN",
    "lunch": "LUNCH",
}
ADMIN_ROLES = {"ADMIN", "DEPUTY_ADMIN"}
OPERATOR_ROLES = set(ACTIVITIES)

# SYSTEM_SPEC section 4, "Can do" column, written out by hand.
SPEC_ACTIVITY_PAGES = {
    "REGISTRATION": {"REGISTRATION"},
    "THOBE_ALLOCATION": {"THOBE_ALLOCATION"},
    "SEATING": {"SEATING"},
    "QUEUE": {"QUEUE"},
    "STAGE": {"STAGE"},
    "THOBE_RETURN": {"THOBE_RETURN"},
    "LUNCH": {"LUNCH"},
    "ADMIN": set(ACTIVITIES),        # "All seven pages"
    "DEPUTY_ADMIN": set(ACTIVITIES),  # "identical powers"
}
SPEC_ADMIN_CONSOLE = {"ADMIN", "DEPUTY_ADMIN"}  # dashboard, corrections, users, sync ... Admin only

STATIONS = [  # station_id, activity
    ("REG-01", "REGISTRATION"), ("REG-02", "REGISTRATION"),
    ("THO-01", "THOBE_ALLOCATION"), ("SEA-01", "SEATING"), ("QUE-01", "QUEUE"), ("STG-01", "STAGE"),
    ("RET-01", "THOBE_RETURN"), ("LUN-01", "LUNCH"),
]
STATION_FOR_ROLE = {  # the station an operator of this role sits at in the matrix
    "REGISTRATION": "REG-01", "THOBE_ALLOCATION": "THO-01", "SEATING": "SEA-01", "QUEUE": "QUE-01",
    "STAGE": "STG-01", "THOBE_RETURN": "RET-01", "LUNCH": "LUN-01",
}


def slug(activity: str) -> str:
    return activity.lower().replace("_", "-")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_size=10, max_overflow=0, pool_pre_ping=True)
    drop_everything(eng)
    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr
    yield eng
    drop_everything(eng)
    eng.dispose()


@pytest.fixture(scope="module")
def world(engine):
    """Nine accounts (one per identity) and the station layout of all three venues."""
    w = SimpleNamespace(user_ids={}, devices={})
    with engine.begin() as c:
        for key, role in IDENTITIES.items():
            w.user_ids[key] = users_svc.create_user(
                c, username=f"{key}-user", password=PASSWORD, role=role, full_name=key.replace("_", " ").title())
        for station_id, activity in STATIONS:
            stations_svc.create_station(c, venue_id=OWNER[activity], station_id=station_id, activity=activity)
            w.devices[station_id] = stations_svc.bind_station(c, station_id, actor_id=w.user_ids["admin"])
    return w


def build_app(mode, venue=None):
    settings = Settings(mode=mode, venue_id=venue, database_url=TEST_DB_URL)
    return create_app(settings=settings)


@pytest.fixture(scope="module")
def apps(engine, world):
    built = {v: build_app("venue", v) for v in VENUES}
    built["central"] = build_app("central")
    for app in built.values():  # a stand-in for "any later write endpoint": auth + ownership guard
        @app.post("/_probe/{activity}", dependencies=[Depends(require_user), Depends(require_can_originate())])
        def probe(activity: str):
            return {"ok": True}
    return built


def new_client(app, device=None):
    client = TestClient(app)
    if device:
        client.cookies.set(DEVICE_COOKIE, device, domain=COOKIE_DOMAIN)
    return client


def api_login(client, username, password=PASSWORD, **extra):
    return client.post("/api/login", json={"username": username, "password": password, **extra})


_CLIENTS = {}


def signed_in(apps, world, venue, key):
    """A cached, logged-in client for identity `key` at venue `venue`."""
    if (venue, key) not in _CLIENTS:
        role = IDENTITIES[key]
        device = world.devices[STATION_FOR_ROLE[role]] if role in OPERATOR_ROLES else None
        client = new_client(apps[venue], device)
        response = api_login(client, f"{key}-user")
        assert response.status_code == 200, response.text
        _CLIENTS[(venue, key)] = client
    return _CLIENTS[(venue, key)]


def fresh_user(engine, role="LUNCH", password=PASSWORD, active=True, prefix="tmp"):
    username = f"{prefix}-{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        uid = users_svc.create_user(c, username=username, password=password, role=role)
        if not active:
            admin_id = c.execute(text("SELECT id FROM users WHERE username='admin-user'")).scalar_one()
            users_svc.set_user_active(c, uid, False, actor_id=admin_id)
    return username, uid


def session_row(engine, token):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM sessions WHERE token_hash=:h"), {"h": sha256(token)}).mappings().one_or_none()


def age_session(engine, token, *, idle_minutes=None, expires_in_seconds=None):
    with engine.begin() as c:
        if idle_minutes is not None:
            c.execute(text("UPDATE sessions SET last_seen_at = now() - make_interval(mins => :m) WHERE token_hash=:h"),
                      {"m": idle_minutes, "h": sha256(token)})
        if expires_in_seconds is not None:
            c.execute(text("UPDATE sessions SET expires_at = now() + make_interval(secs => :s) WHERE token_hash=:h"),
                      {"s": expires_in_seconds, "h": sha256(token)})


def redirect_query(response):
    assert response.status_code == 303, (response.status_code, response.text[:200])
    return parse_qs(urlparse(response.headers["location"]).query)


def detail_code(response):
    return response.json()["detail"]["code"]


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
class TestPasswordHashing:
    def test_argon2id_is_used(self):
        assert passwords.hash_password(PASSWORD).startswith("$argon2id$")

    def test_correct_password_verifies_and_wrong_one_does_not(self):
        h = passwords.hash_password(PASSWORD)
        assert passwords.verify_password(PASSWORD, h) is True
        assert passwords.verify_password(PASSWORD + "x", h) is False
        assert passwords.verify_password("", h) is False

    def test_same_password_gets_a_different_hash_each_time(self):
        assert passwords.hash_password(PASSWORD) != passwords.hash_password(PASSWORD)

    def test_long_passwords_are_not_silently_truncated(self):
        long_pw = "a" * 100 + "-first"
        h = passwords.hash_password(long_pw)
        assert passwords.verify_password("a" * 100 + "-other", h) is False  # bcrypt would accept this

    def test_a_garbage_hash_never_verifies_and_never_raises(self):
        assert passwords.verify_password(PASSWORD, "not-a-hash") is False

    def test_stored_hash_is_never_the_plaintext(self, engine, world):
        with engine.connect() as c:
            stored = c.execute(text("SELECT password_hash FROM users WHERE username='admin-user'")).scalar_one()
        assert stored.startswith("$argon2id$") and PASSWORD not in stored

    @pytest.mark.parametrize("password,role,ok", [
        ("short", "LUNCH", False), ("1234567", "SEATING", False), ("12345678", "SEATING", True),
        ("        ", "QUEUE", False),
        ("Twelve-chars", "ADMIN", True), ("Eleven-char", "ADMIN", False),
        ("Eleven-char", "DEPUTY_ADMIN", False), ("Twelve-chars", "DEPUTY_ADMIN", True),
    ])
    def test_password_policy(self, password, role, ok):
        if ok:
            passwords.validate_password(password, role)
        else:
            with pytest.raises(passwords.WeakPasswordError):
                passwords.validate_password(password, role)


# --------------------------------------------------------------------------- #
# Role model: identical Admin / Deputy powers, spec section 4
# --------------------------------------------------------------------------- #
class TestRoleModel:
    def test_there_is_one_role_per_spec_row_plus_the_deputy(self):
        assert set(permissions.ALL_ROLES) == set(IDENTITIES.values())
        assert len(permissions.ALL_ROLES) == 9

    def test_admin_and_deputy_have_exactly_the_same_permissions(self):
        assert permissions.permissions_for("ADMIN") == permissions.permissions_for("DEPUTY_ADMIN")

    @pytest.mark.parametrize("role", sorted(OPERATOR_ROLES))
    def test_an_operator_has_only_their_own_activity_and_no_admin_power(self, role):
        perms = permissions.permissions_for(role)
        assert perms == {permissions.activity_permission(role)}
        assert not permissions.has_permission(role, permissions.ADMIN_PERMISSION)

    @pytest.mark.parametrize("role", sorted(IDENTITIES.values()))
    def test_permissions_match_the_hand_written_spec_table(self, role):
        for activity in ACTIVITIES:
            assert permissions.can_use_activity(role, activity) == (activity in SPEC_ACTIVITY_PAGES[role])
        assert permissions.has_permission(role, permissions.ADMIN_PERMISSION) == (role in SPEC_ADMIN_CONSOLE)

    def test_unknown_role_has_no_permissions(self):
        assert permissions.permissions_for("SUPERUSER") == frozenset()


# --------------------------------------------------------------------------- #
# Login, disabled users, sessions
# --------------------------------------------------------------------------- #
class TestLogin:
    def test_wrong_password_and_unknown_user_get_the_same_answer(self, apps, world):
        client = new_client(apps["college"])
        wrong_pw = api_login(client, "admin-user", "Definitely-wrong-1")
        no_user = api_login(client, "nobody-here", PASSWORD)
        assert wrong_pw.status_code == no_user.status_code == 401
        assert detail_code(wrong_pw) == detail_code(no_user) == "BAD_CREDENTIALS"
        assert wrong_pw.json() == no_user.json()  # cannot tell which usernames exist

    def test_login_is_case_insensitive_on_username(self, apps, world):
        assert api_login(new_client(apps["college"]), "ADMIN-USER").status_code == 200

    def test_disabled_user_cannot_log_in_even_with_the_correct_password(self, apps, engine, world):
        username, _ = fresh_user(engine, role="ADMIN", active=False)
        response = api_login(new_client(apps["hall"]), username, PASSWORD)
        assert response.status_code == 403
        assert detail_code(response) == "ACCOUNT_DISABLED"

    def test_disabled_operator_cannot_log_in_at_a_bound_station(self, apps, engine, world):
        username, _ = fresh_user(engine, role="LUNCH", active=False)
        response = api_login(new_client(apps["hall"], world.devices["LUN-01"]), username)
        assert response.status_code == 403 and detail_code(response) == "ACCOUNT_DISABLED"

    def test_a_disabled_account_does_not_reveal_itself_to_a_wrong_password(self, apps, engine, world):
        username, _ = fresh_user(engine, role="ADMIN", active=False)
        response = api_login(new_client(apps["hall"]), username, "Definitely-wrong-1")
        assert response.status_code == 401 and detail_code(response) == "BAD_CREDENTIALS"

    def test_disabling_a_user_ends_their_existing_session_immediately(self, apps, engine, world):
        username, uid = fresh_user(engine, role="LUNCH")
        client = new_client(apps["hall"], world.devices["LUN-01"])
        assert api_login(client, username).status_code == 200
        assert client.get("/api/me").status_code == 200
        with engine.begin() as c:
            users_svc.set_user_active(c, uid, False, actor_id=world.user_ids["admin"])
        assert client.get("/api/me").status_code == 401
        with engine.begin() as c:
            users_svc.set_user_active(c, uid, True, actor_id=world.user_ids["admin"])
        assert api_login(new_client(apps["hall"], world.devices["LUN-01"]), username).status_code == 200

    def test_a_switched_off_account_is_refused_even_if_no_one_revoked_its_session(self, apps, engine, world):
        # Second layer: validity is re-checked against the user row on every request, so a direct
        # database change (or a code path that forgets to revoke) still cuts the session off.
        username, uid = fresh_user(engine, role="LUNCH")
        client = new_client(apps["hall"], world.devices["LUN-01"])
        api_login(client, username)
        assert client.get("/api/me").status_code == 200
        with engine.begin() as c:
            c.execute(text("UPDATE users SET active = false WHERE id = :i"), {"i": uid})
        assert client.get("/api/me").status_code == 401

    def test_login_reports_who_and_where_but_never_the_password_hash(self, apps, world):
        response = api_login(new_client(apps["college"], world.devices["REG-01"]), "registration-user")
        body = response.json()
        assert body["user"]["role"] == "REGISTRATION" and body["station_id"] == "REG-01"
        assert body["activity"] == "REGISTRATION"
        assert "password" not in response.text.lower() and "argon2" not in response.text

    def test_the_browser_form_login_sets_a_cookie_and_redirects(self, apps, world):
        client = new_client(apps["college"], world.devices["REG-01"])
        response = client.post("/login", data={"username": "registration-user", "password": PASSWORD},
                               follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/station/registration"
        assert SESSION_COOKIE in client.cookies
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=strict" in set_cookie

    def test_the_form_login_page_and_failed_form_login_show_a_plain_sentence(self, apps):
        client = new_client(apps["college"])
        assert client.get("/login").status_code == 200
        failed = client.post("/login", data={"username": "admin-user", "password": "nope-nope-nope"})
        assert failed.status_code == 401 and "Wrong username or password." in failed.text

    def test_admin_lands_on_the_admin_console_and_root_redirects_by_role(self, apps, world):
        admin = signed_in(apps, world, "college", "admin")
        assert admin.get("/", follow_redirects=False).headers["location"] == "/admin"
        operator = signed_in(apps, world, "stadium", "seating")
        assert operator.get("/", follow_redirects=False).headers["location"] == "/station/seating"
        assert new_client(apps["college"]).get("/", follow_redirects=False).headers["location"] == "/login"


class TestSessions:
    def _login(self, apps, world, engine, role="LUNCH", venue="hall", station="LUN-01"):
        username, _ = fresh_user(engine, role=role)
        client = new_client(apps[venue], world.devices[station])
        response = api_login(client, username)
        assert response.status_code == 200
        return client, response.json()["token"]

    def test_documented_timeouts_are_the_defaults(self):
        settings = Settings(mode="venue", venue_id="hall", database_url=TEST_DB_URL)
        assert settings.session_idle_minutes == 120   # 2 hours idle
        assert settings.session_max_hours == 12       # one event day

    def test_token_is_stored_only_as_a_hash(self, apps, world, engine):
        _, token = self._login(apps, world, engine)
        row = session_row(engine, token)
        assert row is not None and row["token_hash"] == sha256(token) and token not in row["token_hash"]
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM sessions WHERE token_hash=:t"), {"t": token}).scalar_one() == 0

    def test_absolute_lifetime_is_twelve_hours(self, apps, world, engine):
        _, token = self._login(apps, world, engine)
        with engine.connect() as c:
            hours = c.execute(text("SELECT extract(epoch FROM (expires_at - created_at))/3600 FROM sessions WHERE token_hash=:h"),
                              {"h": sha256(token)}).scalar_one()
        assert 11.99 < float(hours) < 12.01

    def test_a_session_survives_just_under_the_idle_limit_and_slides(self, apps, world, engine):
        client, token = self._login(apps, world, engine)
        age_session(engine, token, idle_minutes=119)
        assert client.get("/api/me").status_code == 200
        with engine.connect() as c:  # the request renewed it
            idle = c.execute(text("SELECT extract(epoch FROM (now() - last_seen_at))/60 FROM sessions WHERE token_hash=:h"),
                             {"h": sha256(token)}).scalar_one()
        assert float(idle) < 2

    def test_a_session_idle_for_more_than_two_hours_expires(self, apps, world, engine):
        client, token = self._login(apps, world, engine)
        age_session(engine, token, idle_minutes=121)
        response = client.get("/api/me")
        assert response.status_code == 401 and detail_code(response) == "SESSION_EXPIRED"
        assert "session has ended" in response.json()["detail"]["message"].lower()

    def test_an_active_session_still_dies_at_the_absolute_limit(self, apps, world, engine):
        client, token = self._login(apps, world, engine)
        age_session(engine, token, expires_in_seconds=-1)
        assert client.get("/api/me").status_code == 401

    def test_logout_ends_the_session_and_the_token_cannot_be_reused(self, apps, world, engine):
        client, token = self._login(apps, world, engine)
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/me").status_code == 401
        replay = new_client(apps["hall"])
        assert replay.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_bearer_token_works_like_the_cookie(self, apps, world, engine):
        _, token = self._login(apps, world, engine)
        other = new_client(apps["hall"])
        response = other.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200 and response.json()["role"] == "LUNCH"

    def test_garbage_tokens_are_rejected_plainly(self, apps):
        client = new_client(apps["hall"])
        for headers in ({"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}, {}):
            response = client.get("/api/me", headers=headers)
            assert response.status_code == 401 and detail_code(response) in {"NOT_SIGNED_IN", "SESSION_EXPIRED"}

    def test_new_login_clears_out_long_dead_sessions(self, apps, world, engine):
        _, old = self._login(apps, world, engine)
        with engine.begin() as c:
            c.execute(text("UPDATE sessions SET expires_at = now() - interval '3 days' WHERE token_hash=:h"),
                      {"h": sha256(old)})
        self._login(apps, world, engine)
        assert session_row(engine, old) is None


# --------------------------------------------------------------------------- #
# The role matrix: 9 identities x every protected endpoint, per venue server
# --------------------------------------------------------------------------- #
STATION_PAGES = [("GET", f"/station/{slug(a)}") for a in ACTIVITIES]
ADMIN_ENDPOINTS = [
    ("GET", "/admin"), ("GET", "/admin/users"), ("GET", "/admin/stations"), ("GET", "/admin/bind"),
    ("POST", "/admin/users"), ("POST", "/admin/stations"),
    ("POST", "/admin/import/preview"), ("POST", "/admin/import/commit"),
    ("POST", "/admin/photos/link"), ("POST", "/admin/master-pack/import"),
]
ENDPOINTS = [("GET", "/api/me")] + STATION_PAGES + ADMIN_ENDPOINTS


def _viable(venue, key):
    role = IDENTITIES[key]
    return role in ADMIN_ROLES or OWNER[role] == venue


def expected_allowed(role, venue, path):
    """Spec section 4 + section 11.2, independent of the implementation."""
    if path == "/api/me":
        return True
    if path.startswith("/station/"):
        activity = next(a for a in ACTIVITIES if slug(a) == path.rsplit("/", 1)[1])
        return activity in SPEC_ACTIVITY_PAGES[role] and OWNER[activity] == venue
    return role in SPEC_ADMIN_CONSOLE


MATRIX = [
    pytest.param(venue, key, method, path, id=f"{venue}-{key}-{method}-{path}")
    for venue in VENUES for key in IDENTITIES if _viable(venue, key) for method, path in ENDPOINTS
]


class TestRoleMatrix:
    @pytest.mark.parametrize("venue,key,method,path", MATRIX)
    def test_each_role_reaches_exactly_its_own_endpoints(self, apps, world, venue, key, method, path):
        client = signed_in(apps, world, venue, key)
        response = client.request(method, path)
        role = IDENTITIES[key]
        if expected_allowed(role, venue, path):
            assert response.status_code not in (401, 403), (response.status_code, response.text[:200])
        else:
            assert response.status_code == 403, (response.status_code, response.text[:200])

    @pytest.mark.parametrize("venue", VENUES)
    @pytest.mark.parametrize("method,path", ENDPOINTS[1:])
    def test_anonymous_callers_get_401_everywhere(self, apps, venue, method, path):
        assert new_client(apps[venue]).request(method, path).status_code == 401

    @pytest.mark.parametrize("venue", VENUES)
    def test_admin_and_deputy_get_identical_answers_to_every_request(self, apps, world, venue):
        statuses = {}
        for key in ("admin", "deputy"):
            client = signed_in(apps, world, venue, key)
            statuses[key] = [client.request(m, p).status_code for m, p in ENDPOINTS]
        assert statuses["admin"] == statuses["deputy"]
        assert 403 in statuses["admin"]  # (a venue does not own every activity: WRONG_VENUE)
        assert 200 in statuses["admin"]

    def test_a_registration_operator_reaches_only_registration(self, apps, world):
        client = signed_in(apps, world, "college", "registration")
        assert client.get("/station/registration").status_code == 200
        for other in ACTIVITIES[1:]:
            assert client.get(f"/station/{slug(other)}").status_code == 403
        for _, path in ADMIN_ENDPOINTS:
            assert client.get(path).status_code in (403, 405)

    @pytest.mark.parametrize("key", [k for k, r in IDENTITIES.items() if r in OPERATOR_ROLES])
    def test_operators_are_locked_out_of_the_phase_3_admin_endpoints(self, apps, world, key):
        venue = OWNER[IDENTITIES[key]]
        client = signed_in(apps, world, venue, key)
        assert client.post("/admin/snapshot/freeze").status_code == 403
        assert client.get("/admin/master-pack/export").status_code == 403
        assert client.post("/admin/import/commit").status_code == 403

    @pytest.mark.parametrize("key", ["admin", "deputy"])
    def test_admin_and_deputy_can_use_the_phase_3_endpoints(self, apps, world, key):
        client = signed_in(apps, world, "college", key)
        assert client.post("/admin/snapshot/freeze").status_code == 200
        assert client.post("/admin/photos/link").status_code == 422  # reached the handler: form is empty

    def test_every_route_is_protected_unless_deliberately_public(self, engine, world):
        app = build_app("venue", "college")
        public = {("GET", "/"), ("GET", "/login"), ("POST", "/login"), ("POST", "/api/login"), ("GET", "/health"),
                  # Phase 11: the audience screen. Deliberately public; serves only the approved LED payload
                  # and exists only at the Stadium (tests/test_stage.py::TestPublicLed).
                  ("GET", "/led"), ("GET", "/led/state"), ("GET", "/led/events"), ("GET", "/led/photo/{key}")}
        visited = set()
        # OpenAPI lists every route however it was mounted (included routers are nested objects).
        for template, operations in app.openapi()["paths"].items():
            path = re.sub(r"\{user_id\}", str(uuid.uuid4()), template)
            path = re.sub(r"\{[^}]+\}", "x", path)
            for method in sorted(m.upper() for m in operations if m in {"get", "post", "put", "patch", "delete"}):
                if (method, template) in public:
                    continue
                response = new_client(app).request(method, path)
                assert response.status_code == 401, f"{method} {template} is reachable without signing in"
                visited.add((method, template))
        # The sweep must really have reached the admin, station and account routes (a silently
        # empty sweep once looked like a pass), including every Phase 3 admin endpoint.
        assert {("GET", "/api/me"), ("GET", "/station/{activity}"), ("POST", "/admin/bind/{station_id}"),
                ("POST", "/admin/users/{user_id}/active"), ("POST", "/admin/snapshot/freeze"),
                ("GET", "/admin/master-pack/export"), ("POST", "/logout")} <= visited
        assert len(visited) >= 20


# --------------------------------------------------------------------------- #
# Stations: one venue, one activity, bound to one laptop
# --------------------------------------------------------------------------- #
class TestStationModel:
    def test_station_creation_rejects_an_activity_the_venue_does_not_own(self, engine):
        with engine.begin() as c:
            with pytest.raises(users_svc.AccountError) as exc:
                stations_svc.create_station(c, venue_id="college", station_id="X-LUN", activity="LUNCH")
        assert exc.value.code == "WRONG_VENUE"

    def test_a_station_can_never_change_venue_or_activity(self, engine, world):
        with engine.connect() as conn:
            outer = conn.begin()
            try:
                with db_error(conn, RESTRICT_VIOLATION):
                    conn.execute(text("UPDATE stations SET activity='SEATING' WHERE station_id='REG-01'"))
                with db_error(conn, RESTRICT_VIOLATION):
                    conn.execute(text("UPDATE stations SET venue_id='hall' WHERE station_id='THO-01'"))
                with db_error(conn, RESTRICT_VIOLATION):
                    conn.execute(text("UPDATE stations SET station_id='RENAMED' WHERE station_id='REG-01'"))
            finally:
                outer.rollback()

    def test_one_laptop_cannot_be_bound_to_two_stations_at_the_database_level(self, engine, world):
        with engine.connect() as conn:
            outer = conn.begin()
            try:
                token_hash = conn.execute(text("SELECT device_token_hash FROM stations WHERE station_id='REG-01'")).scalar_one()
                with db_error(conn, UNIQUE_VIOLATION):
                    conn.execute(text("UPDATE stations SET device_token_hash=:h, bound_at=now(), bound_by=:u "
                                      "WHERE station_id='REG-02'"), {"h": token_hash, "u": world.user_ids["admin"]})
            finally:
                outer.rollback()

    def test_a_binding_always_records_when_and_by_whom(self, engine, world):
        with engine.connect() as conn:
            outer = conn.begin()
            try:
                with db_error(conn, CHECK_VIOLATION):  # a hash with no bound_at / bound_by
                    conn.execute(text("UPDATE stations SET bound_at=NULL WHERE station_id='REG-01'"))
                with db_error(conn, CHECK_VIOLATION):  # a binding time with no hash
                    conn.execute(text("UPDATE stations SET device_token_hash=NULL WHERE station_id='REG-01'"))
            finally:
                outer.rollback()

    def test_the_device_token_is_stored_hashed(self, engine, world):
        with engine.connect() as c:
            stored = c.execute(text("SELECT device_token_hash FROM stations WHERE station_id='REG-01'")).scalar_one()
        assert stored == sha256(world.devices["REG-01"]) and stored != world.devices["REG-01"]

    def test_operator_activity_comes_from_the_station_not_from_the_operator(self, apps, world):
        client = new_client(apps["hall"], world.devices["LUN-01"])
        response = api_login(client, "lunch-user", activity="THOBE_RETURN", station_id="RET-01")  # ignored
        assert response.status_code == 200
        assert response.json()["station_id"] == "LUN-01" and response.json()["activity"] == "LUNCH"
        me = client.get("/api/me").json()
        assert me["activity"] == "LUNCH" and me["station_id"] == "LUN-01"
        assert client.get("/station/thobe-return").status_code == 403

    def test_what_an_operator_can_reach_follows_the_station_binding_itself(self, apps, world, engine):
        # Isolate the binding: point a LUNCH operator's session at a THOBE_RETURN station. The
        # role still says LUNCH, but the station now decides, so the Lunch screen is refused.
        username, _ = fresh_user(engine, role="LUNCH")
        client = new_client(apps["hall"], world.devices["LUN-01"])
        token = api_login(client, username).json()["token"]
        assert client.get("/station/lunch").status_code == 200
        with engine.begin() as c:
            c.execute(text("UPDATE sessions SET station_id = 'RET-01' WHERE token_hash = :h"), {"h": sha256(token)})
        response = client.get("/station/lunch")
        assert response.status_code == 403 and detail_code(response) == "STATION_MISMATCH"
        assert client.get("/api/me").json()["activity"] == "THOBE_RETURN"

    def test_the_station_page_shows_the_bound_station_and_operator(self, apps, world):
        page = signed_in(apps, world, "stadium", "seating").get("/station/seating")
        assert page.status_code == 200 and "SEA-01" in page.text and "Seating" in page.text

    def test_an_operator_cannot_sign_in_at_a_station_for_another_activity(self, apps, world):
        client = new_client(apps["hall"], world.devices["RET-01"])  # a THOBE_RETURN station
        response = api_login(client, "lunch-user")                  # a LUNCH operator
        assert response.status_code == 403 and detail_code(response) == "STATION_MISMATCH"

    def test_an_operator_on_an_unbound_laptop_cannot_sign_in(self, apps, world):
        response = api_login(new_client(apps["hall"]), "lunch-user")
        assert response.status_code == 403 and detail_code(response) == "NO_STATION"

    def test_a_laptop_bound_at_another_venue_is_unbound_here(self, apps, world):
        response = api_login(new_client(apps["college"], world.devices["LUN-01"]), "registration-user")
        assert response.status_code == 403 and detail_code(response) == "NO_STATION"

    def test_a_forged_device_cookie_is_treated_as_unbound(self, apps):
        response = api_login(new_client(apps["hall"], "f" * 43), "lunch-user")
        assert response.status_code == 403 and detail_code(response) == "NO_STATION"

    def test_a_deactivated_station_locks_out_its_operator_at_once(self, apps, world, engine):
        with engine.begin() as c:
            stations_svc.create_station(c, venue_id="hall", station_id="LUN-TMP", activity="LUNCH")
            device = stations_svc.bind_station(c, "LUN-TMP", actor_id=world.user_ids["admin"])
        username, _ = fresh_user(engine, role="LUNCH")
        client = new_client(apps["hall"], device)
        assert api_login(client, username).status_code == 200
        with engine.begin() as c:
            stations_svc.set_station_active(c, "LUN-TMP", False, actor_id=world.user_ids["admin"])
        assert client.get("/api/me").status_code == 401
        assert detail_code(api_login(new_client(apps["hall"], device), username)) == "NO_STATION"

    def test_admin_may_sign_in_on_any_laptop_bound_or_not(self, apps, world):
        assert api_login(new_client(apps["hall"]), "admin-user").status_code == 200
        bound = api_login(new_client(apps["college"], world.devices["REG-01"]), "deputy-user")
        assert bound.status_code == 200 and bound.json()["activity"] is None  # admin is not limited by the station


class TestBindingFlow:
    """Admin action, meant to be finished in a handful of taps when a laptop dies."""

    def _admin(self, apps, venue="college"):
        client = new_client(apps[venue])
        assert api_login(client, "admin-user").status_code == 200
        return client

    def test_rebinding_a_spare_laptop_takes_three_requests(self, apps, world, engine):
        started = time.monotonic()
        spare = self._admin(apps)                                               # 1. Admin signs in on the spare
        bind = spare.post("/admin/bind/REG-02", follow_redirects=False)         # 2. taps the station
        assert bind.status_code == 303 and "msg" in redirect_query(bind)
        assert DEVICE_COOKIE in bind.headers["set-cookie"]
        device = spare.cookies.get(DEVICE_COOKIE)
        username, _ = fresh_user(engine, role="REGISTRATION")
        operator = new_client(apps["college"], device)
        response = api_login(operator, username)                                # 3. operator signs in
        assert response.status_code == 200 and response.json()["station_id"] == "REG-02"
        assert time.monotonic() - started < 30
        world.devices["REG-02"] = device  # keep later tests consistent

    def test_the_bind_page_lists_stations_in_big_buttons_and_shows_this_laptop(self, apps, world):
        admin = self._admin(apps)
        page = admin.get("/admin/bind")
        assert page.status_code == 200
        for station_id in ("REG-01", "REG-02"):
            assert station_id in page.text
        assert "THO-01" not in page.text  # a College server only lists College stations

    def test_rebinding_a_station_retires_the_old_laptop_and_its_session(self, apps, world, engine):
        old_device = world.devices["THO-01"]
        username, _ = fresh_user(engine, role="THOBE_ALLOCATION")
        old_laptop = new_client(apps["stadium"], old_device)
        assert api_login(old_laptop, username).status_code == 200
        spare = new_client(apps["stadium"])
        api_login(spare, "admin-user")
        assert spare.post("/admin/bind/THO-01", follow_redirects=False).status_code == 303
        new_device = spare.cookies.get(DEVICE_COOKIE)
        assert new_device and new_device != old_device
        assert old_laptop.get("/api/me").status_code == 401                         # kicked off
        assert detail_code(api_login(new_client(apps["stadium"], old_device), username)) == "NO_STATION"
        assert api_login(new_client(apps["stadium"], new_device), username).status_code == 200
        world.devices["THO-01"] = new_device

    def test_binding_a_laptop_to_a_new_station_frees_its_old_station(self, apps, world, engine):
        with engine.begin() as c:
            stations_svc.create_station(c, venue_id="stadium", station_id="QUE-02", activity="QUEUE")
            stations_svc.create_station(c, venue_id="stadium", station_id="QUE-03", activity="QUEUE")
        laptop = new_client(apps["stadium"])
        api_login(laptop, "admin-user")
        laptop.post("/admin/bind/QUE-02", follow_redirects=False)
        first = laptop.cookies.get(DEVICE_COOKIE)
        laptop.post("/admin/bind/QUE-03", follow_redirects=False)
        with engine.connect() as c:
            rows = dict(c.execute(text("SELECT station_id, device_token_hash FROM stations WHERE station_id IN ('QUE-02','QUE-03')")).all())
        assert rows["QUE-02"] is None and rows["QUE-03"] == sha256(laptop.cookies.get(DEVICE_COOKIE))
        assert laptop.cookies.get(DEVICE_COOKIE) != first

    def test_unbinding_a_station_leaves_no_working_laptop(self, apps, world, engine):
        with engine.begin() as c:
            stations_svc.create_station(c, venue_id="hall", station_id="LUN-02", activity="LUNCH")
            device = stations_svc.bind_station(c, "LUN-02", actor_id=world.user_ids["admin"])
        admin = self._admin(apps, "hall")
        assert admin.post("/admin/unbind/LUN-02", follow_redirects=False).status_code == 303
        username, _ = fresh_user(engine, role="LUNCH")
        assert detail_code(api_login(new_client(apps["hall"], device), username)) == "NO_STATION"

    def test_binding_an_inactive_or_foreign_station_is_refused_with_a_plain_message(self, apps, world, engine):
        admin = self._admin(apps, "college")
        foreign = admin.post("/admin/bind/LUN-01", follow_redirects=False)         # a Hall station on the College server
        assert "error" in redirect_query(foreign)
        with engine.begin() as c:
            stations_svc.create_station(c, venue_id="college", station_id="REG-OFF", activity="REGISTRATION")
            stations_svc.set_station_active(c, "REG-OFF", False, actor_id=world.user_ids["admin"])
        inactive = admin.post("/admin/bind/REG-OFF", follow_redirects=False)
        query = redirect_query(inactive)
        assert "error" in query and query["error"][0].endswith(".")
        with engine.connect() as c:  # nothing was bound by the refused requests
            assert c.execute(text("SELECT device_token_hash FROM stations WHERE station_id='REG-OFF'")).scalar_one() is None
            assert c.execute(text("SELECT device_token_hash FROM stations WHERE station_id='LUN-01'")).scalar_one() == sha256(world.devices["LUN-01"])

    def test_binding_and_unbinding_are_audited(self, apps, world, engine):
        with engine.connect() as c:
            actions = {r[0] for r in c.execute(text("SELECT action FROM audit_log WHERE operator_id=:u"), {"u": world.user_ids["admin"]})}
        assert {"STATION_BOUND", "STATION_UNBOUND"} <= actions


# --------------------------------------------------------------------------- #
# Venue ownership: golden rule 4, service level and as a reusable dependency
# --------------------------------------------------------------------------- #
class TestVenueOwnershipService:
    def guard(self, venue):
        return ownership.VenueGuard(mode="venue", venue_id=venue)

    def test_the_map_matches_the_database_function(self, engine, world):
        with engine.connect() as c:
            db_map = {a: c.execute(text("SELECT activity_owner(:a)"), {"a": a}).scalar_one() for a in ACTIVITIES}
        assert ownership.ACTIVITY_OWNER == db_map == OWNER

    def test_hall_rejects_registration(self):
        with pytest.raises(ownership.WrongVenueError):
            self.guard("hall").ensure_can_originate("REGISTRATION")

    @pytest.mark.parametrize("activity", ["THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE", "THOBE_RETURN", "LUNCH"])
    def test_college_rejects_everything_but_registration(self, activity):
        with pytest.raises(ownership.WrongVenueError):
            self.guard("college").ensure_can_originate(activity)

    @pytest.mark.parametrize("activity", ["REGISTRATION", "THOBE_RETURN", "LUNCH"])
    def test_stadium_rejects_registration_and_the_hall_activities(self, activity):
        with pytest.raises(ownership.WrongVenueError):
            self.guard("stadium").ensure_can_originate(activity)

    @pytest.mark.parametrize("venue", VENUES)
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_every_venue_accepts_exactly_its_own_activities(self, venue, activity):
        guard = self.guard(venue)
        if OWNER[activity] == venue:
            guard.ensure_can_originate(activity)
            assert guard.owns(activity)
        else:
            with pytest.raises(ownership.WrongVenueError) as exc:
                guard.ensure_can_originate(activity)
            assert exc.value.owner == OWNER[activity] and not guard.owns(activity)

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_central_never_originates_a_venue_owned_write(self, activity):
        central = ownership.VenueGuard(mode="central", venue_id=None)
        with pytest.raises(ownership.CentralCannotOriginateError):
            central.ensure_can_originate(activity)
        assert central.owns(activity) is False

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_central_accepts_corrections_by_routing_them_to_the_owning_venue(self, activity):
        route = ownership.VenueGuard(mode="central", venue_id=None).route_correction(activity)
        assert route.apply_here is False and route.owner_venue == OWNER[activity]

    @pytest.mark.parametrize("venue", VENUES)
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_a_venue_applies_its_own_corrections_and_forwards_the_rest(self, venue, activity):
        route = self.guard(venue).route_correction(activity)
        assert route.owner_venue == OWNER[activity] and route.apply_here == (OWNER[activity] == venue)

    def test_unknown_activities_are_refused(self):
        with pytest.raises(ownership.UnknownActivityError):
            self.guard("hall").ensure_can_originate("EXIT")

    def test_the_guard_builds_from_settings(self):
        guard = ownership.guard_from_settings(Settings(mode="venue", venue_id="stadium", database_url=TEST_DB_URL))
        assert guard.owns("QUEUE") and not guard.owns("LUNCH")

    def test_rejection_messages_are_one_plain_sentence(self):
        with pytest.raises(ownership.WrongVenueError) as exc:
            self.guard("hall").ensure_can_originate("REGISTRATION")
        message = exc.value.message
        assert message == "Registration is recorded at the College server, not here."
        with pytest.raises(ownership.CentralCannotOriginateError) as exc:
            ownership.VenueGuard(mode="central", venue_id=None).ensure_can_originate("LUNCH")
        assert "\n" not in exc.value.message and exc.value.message.endswith(".")


class TestVenueOwnershipHttp:
    """The same guard, used as a dependency on a stand-in write endpoint (`/_probe/{activity}`)."""

    @pytest.mark.parametrize("venue", VENUES)
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_write_path_dependency_accepts_owned_and_rejects_foreign_activities(self, apps, world, venue, activity):
        response = signed_in(apps, world, venue, "admin").post(f"/_probe/{slug(activity)}")
        if OWNER[activity] == venue:
            assert response.status_code == 200
        else:
            assert response.status_code == 403 and detail_code(response) == "WRONG_VENUE"
            assert "not here" in response.json()["detail"]["message"]

    def test_central_rejects_every_write_even_from_the_admin(self, apps, world):
        admin = signed_in(apps, world, "central", "admin")
        for activity in ACTIVITIES:
            response = admin.post(f"/_probe/{slug(activity)}")
            assert response.status_code == 403 and detail_code(response) == "CENTRAL_CANNOT_ORIGINATE"

    def test_an_unknown_activity_is_a_404_not_a_500(self, apps, world):
        assert signed_in(apps, world, "hall", "admin").post("/_probe/exit").status_code == 404


class TestCentralMode:
    def test_admin_and_deputy_can_sign_in_and_read(self, apps, world):
        for key in ("admin", "deputy"):
            client = signed_in(apps, world, "central", key)
            me = client.get("/api/me")
            assert me.status_code == 200 and me.json()["role"] == IDENTITIES[key]

    def test_operators_sign_in_at_their_own_venue_not_at_central(self, apps, world):
        client = new_client(apps["central"], world.devices["LUN-01"])
        response = api_login(client, "lunch-user")
        assert response.status_code == 403 and detail_code(response) == "OPERATORS_NOT_HERE"

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_station_screens_do_not_exist_at_central(self, apps, world, activity):
        response = signed_in(apps, world, "central", "admin").get(f"/station/{slug(activity)}")
        assert response.status_code == 403 and detail_code(response) == "CENTRAL_CANNOT_ORIGINATE"

    @pytest.mark.parametrize("path", ["/admin/stations", "/admin/bind"])
    def test_stations_are_managed_on_venue_servers_only(self, apps, world, path):
        response = signed_in(apps, world, "central", "admin").get(path)
        assert response.status_code == 409 and detail_code(response) == "NOT_A_VENUE"

    def test_central_still_serves_the_admin_console_and_user_management(self, apps, world):
        client = signed_in(apps, world, "central", "admin")
        assert client.get("/admin").status_code == 200
        assert client.get("/admin/users").status_code == 200


# --------------------------------------------------------------------------- #
# Admin screens: user management, station management
# --------------------------------------------------------------------------- #
class TestUserManagement:
    def _admin(self, apps, key="admin", venue="college"):
        client = new_client(apps[venue])
        assert api_login(client, f"{key}-user").status_code == 200
        return client

    def _row(self, engine, username):
        with engine.connect() as c:
            return c.execute(text("SELECT * FROM users WHERE lower(username)=lower(:u)"), {"u": username}).mappings().one_or_none()

    def test_admin_creates_an_operator_who_can_then_sign_in(self, apps, world, engine):
        username = f"new-{uuid.uuid4().hex[:6]}"
        response = self._admin(apps).post("/admin/users", data={
            "username": username, "full_name": "Asha Rao", "role": "LUNCH", "password": "lunch-desk-1"},
            follow_redirects=False)
        assert "msg" in redirect_query(response)
        row = self._row(engine, username)
        assert row["role"] == "LUNCH" and row["active"] and row["password_hash"].startswith("$argon2id$")
        assert api_login(new_client(apps["hall"], world.devices["LUN-01"]), username, "lunch-desk-1").status_code == 200

    def test_the_deputy_can_manage_users_exactly_like_the_admin(self, apps, world, engine):
        username = f"dep-{uuid.uuid4().hex[:6]}"
        response = self._admin(apps, "deputy").post("/admin/users", data={
            "username": username, "role": "SEATING", "password": "seating-desk-1"}, follow_redirects=False)
        assert "msg" in redirect_query(response) and self._row(engine, username) is not None

    def test_duplicate_usernames_are_refused_case_insensitively(self, apps, world, engine):
        response = self._admin(apps).post("/admin/users", data={
            "username": "LUNCH-USER", "role": "LUNCH", "password": "another-pass-1"}, follow_redirects=False)
        assert "error" in redirect_query(response)
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM users WHERE lower(username)='lunch-user'")).scalar_one() == 1

    def test_weak_passwords_and_admin_roles_cannot_be_created_from_the_screen(self, apps, world, engine):
        admin = self._admin(apps)
        weak = admin.post("/admin/users", data={"username": "weak-1", "role": "QUEUE", "password": "short"}, follow_redirects=False)
        assert "error" in redirect_query(weak) and self._row(engine, "weak-1") is None
        escalate = admin.post("/admin/users", data={"username": "sneaky-1", "role": "ADMIN", "password": "Twelve-chars-1"}, follow_redirects=False)
        assert "error" in redirect_query(escalate) and self._row(engine, "sneaky-1") is None
        bogus = admin.post("/admin/users", data={"username": "bogus-1", "role": "SUPERUSER", "password": "long-enough-1"}, follow_redirects=False)
        assert "error" in redirect_query(bogus) and self._row(engine, "bogus-1") is None

    def test_deactivate_and_reactivate_from_the_screen(self, apps, world, engine):
        username, uid = fresh_user(engine, role="QUEUE")
        admin = self._admin(apps)
        assert "msg" in redirect_query(admin.post(f"/admin/users/{uid}/active", data={"active": "0"}, follow_redirects=False))
        assert self._row(engine, username)["active"] is False
        assert "msg" in redirect_query(admin.post(f"/admin/users/{uid}/active", data={"active": "1"}, follow_redirects=False))
        assert self._row(engine, username)["active"] is True

    def test_an_admin_cannot_switch_off_their_own_account(self, apps, world, engine):
        admin = self._admin(apps, "deputy")
        response = admin.post(f"/admin/users/{world.user_ids['deputy']}/active", data={"active": "0"}, follow_redirects=False)
        assert "error" in redirect_query(response)
        assert self._row(engine, "deputy-user")["active"] is True

    def test_deputy_can_switch_off_the_admin_and_the_admin_can_come_back_via_the_deputy(self, apps, world, engine):
        deputy = self._admin(apps, "deputy")
        deputy.post(f"/admin/users/{world.user_ids['admin']}/active", data={"active": "0"}, follow_redirects=False)
        assert detail_code(api_login(new_client(apps["college"]), "admin-user")) == "ACCOUNT_DISABLED"
        deputy.post(f"/admin/users/{world.user_ids['admin']}/active", data={"active": "1"}, follow_redirects=False)
        assert api_login(new_client(apps["college"]), "admin-user").status_code == 200

    def test_password_reset_replaces_the_password_and_ends_old_sessions(self, apps, world, engine):
        username, uid = fresh_user(engine, role="LUNCH")
        laptop = new_client(apps["hall"], world.devices["LUN-01"])
        api_login(laptop, username)
        response = self._admin(apps).post(f"/admin/users/{uid}/password", data={"password": "brand-new-pass-1"}, follow_redirects=False)
        assert "msg" in redirect_query(response)
        assert laptop.get("/api/me").status_code == 401
        assert api_login(new_client(apps["hall"], world.devices["LUN-01"]), username, PASSWORD).status_code == 401
        assert api_login(new_client(apps["hall"], world.devices["LUN-01"]), username, "brand-new-pass-1").status_code == 200

    def test_user_actions_are_audited_under_the_individuals_own_login(self, apps, world, engine):
        username, uid = fresh_user(engine, role="STAGE")
        self._admin(apps, "deputy").post(f"/admin/users/{uid}/active", data={"active": "0"}, follow_redirects=False)
        with engine.connect() as c:
            row = c.execute(text("SELECT operator_id, details FROM audit_log WHERE action='USER_DEACTIVATED' "
                                 "AND details->>'username'=:u"), {"u": username}).one()
        assert row.operator_id == world.user_ids["deputy"]

    def test_the_user_list_never_shows_password_hashes(self, apps, world):
        page = self._admin(apps).get("/admin/users")
        assert page.status_code == 200 and "argon2" not in page.text and "lunch-user" in page.text


class TestStationManagement:
    def _admin(self, apps, venue):
        client = new_client(apps[venue])
        api_login(client, "admin-user")
        return client

    def test_admin_creates_a_station_for_an_activity_this_venue_owns(self, apps, world, engine):
        station_id = f"SEA-{uuid.uuid4().hex[:4].upper()}"
        response = self._admin(apps, "stadium").post("/admin/stations", data={"station_id": station_id, "activity": "SEATING"}, follow_redirects=False)
        assert "msg" in redirect_query(response)
        with engine.connect() as c:
            row = c.execute(text("SELECT venue_id, activity, active, device_token_hash FROM stations WHERE station_id=:s"), {"s": station_id}).one()
        assert (row.venue_id, row.activity, row.active, row.device_token_hash) == ("stadium", "SEATING", True, None)

    def test_a_venue_refuses_a_station_for_someone_elses_activity(self, apps, world, engine):
        response = self._admin(apps, "college").post("/admin/stations", data={"station_id": "LUN-BAD", "activity": "LUNCH"}, follow_redirects=False)
        query = redirect_query(response)
        assert "error" in query and query["error"][0].endswith(".")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM stations WHERE station_id='LUN-BAD'")).scalar_one() == 0

    def test_duplicate_and_blank_station_ids_are_refused(self, apps, world):
        admin = self._admin(apps, "college")
        assert "error" in redirect_query(admin.post("/admin/stations", data={"station_id": "REG-01", "activity": "REGISTRATION"}, follow_redirects=False))
        assert "error" in redirect_query(admin.post("/admin/stations", data={"station_id": "  ", "activity": "REGISTRATION"}, follow_redirects=False))

    def test_the_station_list_shows_only_this_venues_stations_and_their_state(self, apps, world):
        page = self._admin(apps, "hall").get("/admin/stations")
        assert "RET-01" in page.text and "LUN-01" in page.text and "REG-01" not in page.text

    def test_station_changes_are_audited(self, apps, world, engine):
        with engine.connect() as c:
            actions = {r[0] for r in c.execute(text("SELECT action FROM audit_log"))}
        assert "STATION_CREATED" in actions


# --------------------------------------------------------------------------- #
# Seed script: credentials come from the environment or a prompt, never the repo
# --------------------------------------------------------------------------- #
SEED_ENV = {
    "SEED_ADMIN_USERNAME": "event.admin", "SEED_ADMIN_PASSWORD": "Admin-Seed-Pass-1",
    "SEED_DEPUTY_USERNAME": "event.deputy", "SEED_DEPUTY_PASSWORD": "Deputy-Seed-Pass-1",
}


class TestSeedScript:
    # Audit rows reference users, so nothing is deleted between tests: each test seeds unique usernames.
    def _env(self, tag):
        return {**SEED_ENV, "SEED_ADMIN_USERNAME": f"admin.{tag}", "SEED_DEPUTY_USERNAME": f"deputy.{tag}"}

    def test_creates_an_admin_and_a_separate_deputy(self, engine):
        tag = uuid.uuid4().hex[:6]
        result = seed_admins(engine, env=self._env(tag))
        assert sorted(result.created) == [f"admin.{tag}", f"deputy.{tag}"] and result.skipped == []
        with engine.connect() as c:
            rows = dict(c.execute(text("SELECT username, role FROM users WHERE username IN (:a, :d)"),
                                  {"a": f"admin.{tag}", "d": f"deputy.{tag}"}).all())
            hashes = c.execute(text("SELECT password_hash FROM users WHERE username=:a"), {"a": f"admin.{tag}"}).scalar_one()
        assert rows == {f"admin.{tag}": "ADMIN", f"deputy.{tag}": "DEPUTY_ADMIN"}
        assert hashes.startswith("$argon2id$") and "Admin-Seed-Pass-1" not in hashes

    def test_seeded_accounts_can_sign_in(self, engine, apps):
        tag = uuid.uuid4().hex[:6]
        seed_admins(engine, env=self._env(tag))
        assert api_login(new_client(apps["college"]), f"admin.{tag}", "Admin-Seed-Pass-1").status_code == 200
        assert api_login(new_client(apps["college"]), f"deputy.{tag}", "Deputy-Seed-Pass-1").status_code == 200

    def test_running_it_twice_changes_nothing(self, engine):
        tag = uuid.uuid4().hex[:6]
        seed_admins(engine, env=self._env(tag))
        with engine.connect() as c:
            before = c.execute(text("SELECT password_hash FROM users WHERE username=:a"), {"a": f"admin.{tag}"}).scalar_one()
        again = seed_admins(engine, env={**self._env(tag), "SEED_ADMIN_PASSWORD": "A-Different-Pass-99"})
        with engine.connect() as c:
            after = c.execute(text("SELECT password_hash FROM users WHERE username=:a"), {"a": f"admin.{tag}"}).scalar_one()
        assert again.created == [] and sorted(again.skipped) == [f"admin.{tag}", f"deputy.{tag}"]
        assert before == after  # an existing account's password is never overwritten by a re-run

    def test_missing_values_are_prompted_for(self, engine):
        tag = uuid.uuid4().hex[:6]
        asked = []

        def prompt(label, secret):
            asked.append((label, secret))
            return {"admin username": f"admin.{tag}", "admin password": "Admin-Seed-Pass-1",
                    "deputy username": f"deputy.{tag}", "deputy password": "Deputy-Seed-Pass-1"}[label]

        seed_admins(engine, env={}, prompt=prompt)
        assert ("admin password", True) in asked and ("admin username", False) in asked

    def test_missing_values_without_a_prompt_fail_naming_the_variables_not_the_values(self, engine):
        with pytest.raises(SeedError) as exc:
            seed_admins(engine, env={"SEED_ADMIN_USERNAME": "only.admin"}, prompt=None)
        message = str(exc.value)
        assert "SEED_ADMIN_PASSWORD" in message and "SEED_DEPUTY_USERNAME" in message and "SEED_DEPUTY_PASSWORD" in message

    def test_weak_or_duplicate_credentials_are_refused_and_nothing_is_written(self, engine):
        tag = uuid.uuid4().hex[:6]
        with pytest.raises(SeedError):
            seed_admins(engine, env={**self._env(tag), "SEED_ADMIN_PASSWORD": "short"})
        with pytest.raises(SeedError):
            seed_admins(engine, env={**self._env(tag), "SEED_DEPUTY_USERNAME": f"admin.{tag}"})
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM users WHERE username LIKE :p"), {"p": f"%.{tag}"}).scalar_one() == 0

    def test_seeding_is_audited_without_recording_any_password(self, engine):
        tag = uuid.uuid4().hex[:6]
        seed_admins(engine, env=self._env(tag))
        with engine.connect() as c:
            details = [str(r[0]) for r in c.execute(text("SELECT details FROM audit_log WHERE action='USER_SEEDED'"))]
        assert any(f"admin.{tag}" in d for d in details)
        assert not any("Seed-Pass" in d for d in details)

    def test_the_command_line_script_works_end_to_end_without_prompting(self, engine):
        tag = uuid.uuid4().hex[:6]
        import os
        env = {**os.environ, **self._env(tag), "DATABASE_URL": TEST_DB_URL, "MODE": "venue", "VENUE_ID": "college"}
        run = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "seed_admins.py")], cwd=REPO_ROOT, env=env,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Admin-Seed-Pass-1" not in run.stdout + run.stderr
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM users WHERE username IN (:a,:d)"), {"a": f"admin.{tag}", "d": f"deputy.{tag}"}).scalar_one() == 2

    def test_the_command_line_script_fails_cleanly_when_it_cannot_prompt(self, engine):
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("SEED_")}
        env.update({"DATABASE_URL": TEST_DB_URL, "MODE": "venue", "VENUE_ID": "college"})
        run = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "seed_admins.py")], cwd=REPO_ROOT, env=env,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        assert run.returncode != 0 and "SEED_ADMIN_PASSWORD" in run.stderr and "Traceback" not in run.stderr

    def test_no_credentials_are_committed_to_env_example(self):
        lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        for line in lines:
            if "SEED_" in line and not line.lstrip().startswith("#"):
                assert line.split("=", 1)[1].strip() == "", f"credential value in .env.example: {line}"
        assert any("SESSION_IDLE_MINUTES" in line for line in lines)

    def test_no_seed_or_test_password_appears_in_tracked_source_or_config(self):
        offenders = []
        for path in list((REPO_ROOT / "backend").rglob("*.py")) + [REPO_ROOT / ".env.example", REPO_ROOT / "scripts" / "seed_admins.py"]:
            content = path.read_text(encoding="utf-8")
            if "Seed-Pass" in content or PASSWORD in content:
                offenders.append(str(path))
        assert offenders == []


# --------------------------------------------------------------------------- #
# Migration 0005 constraints
# --------------------------------------------------------------------------- #
class TestAuthSchema:
    @pytest.fixture
    def conn(self, engine, world):
        connection = engine.connect()
        outer = connection.begin()
        yield connection
        outer.rollback()
        connection.close()

    def test_deputy_role_is_allowed_and_unknown_roles_still_are_not(self, conn):
        conn.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('dep-x', 'h', 'DEPUTY_ADMIN')"))
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('bad-x', 'h', 'SUPERUSER')"))

    def test_session_tokens_are_unique_and_tied_to_a_real_user(self, conn, world):
        conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h1', :u, now() + interval '1 hour')"),
                     {"u": world.user_ids["admin"]})
        with db_error(conn, UNIQUE_VIOLATION):
            conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h1', :u, now() + interval '1 hour')"),
                         {"u": world.user_ids["admin"]})
        with db_error(conn, "23503"):
            conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h2', gen_random_uuid(), now() + interval '1 hour')"))

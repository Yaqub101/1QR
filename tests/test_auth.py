"""Phase 5 - auth, roles, sessions.

Golden rules under test (AGENTS.md, as amended by docs/ARCHITECTURE_PIVOT.md):
  2. The operator's ROLE decides the activity. There is no station binding any more.
  11. Operator messages are one plain sentence.

SYSTEM_SPEC section 4 (roles) is the source of truth. The expected-access table below
is written out by hand from section 4 and does NOT import backend.security.permissions,
so the implementation is checked against the spec, not against itself.

Session policy assumed for a multi-hour event day (documented in the report):
  * idle timeout   120 minutes, sliding (any request renews it)
  * absolute limit  12 hours  (covers a full day; forces a fresh login next day)
"""
import hashlib
import re
import subprocess
import sys
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend import users as users_svc
from backend.config import Settings
from backend.main import create_app
from backend.security import passwords, permissions
from backend.security.deps import require_user
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

ACTIVITIES = [
    "REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED", "SEATING", "QUEUE", "STAGE", "THOBE_RETURN",
    "MONEY_RETURNED", "LUNCH",
]

# key -> role. The approved role/flow redesign: ONE merged Registry operator role covers Reporting,
# Robe Allocation and Robe Return; the other operator roles stay one per activity; a read-only CALLER
# (Phase R3) who performs no activity at all; plus the Central Event Admin and the named deputy. 8 identities.
IDENTITIES = {
    "admin": "ADMIN",
    "deputy": "DEPUTY_ADMIN",
    "registry": "REGISTRY",
    "seating": "SEATING",
    "queue": "QUEUE",
    "stage": "STAGE",
    "lunch": "LUNCH",
    "caller": "CALLER",
}
ADMIN_ROLES = {"ADMIN", "DEPUTY_ADMIN"}
OPERATOR_ROLES = {"REGISTRY", "SEATING", "QUEUE", "STAGE", "LUNCH", "CALLER"}
CALLER_VIEW = "view:caller"
# Who may open the read-only Caller screen (/caller): the Caller, the Stage operator and the Admins.
SPEC_CALLER_SCREEN = {"CALLER", "STAGE", "ADMIN", "DEPUTY_ADMIN"}
RETIRED_ROLES = ("REGISTRATION", "THOBE_ALLOCATION", "THOBE_RETURN")  # merged into REGISTRY

# Which operator role performs each activity, written out by hand (not imported from the code).
OPERATOR_ROLE_FOR = {
    "REGISTRATION": "REGISTRY",
    "THOBE_ALLOCATION": "REGISTRY",
    "MONEY_RECEIVED": "REGISTRY",
    "SEATING": "SEATING",
    "QUEUE": "QUEUE",
    "STAGE": "STAGE",
    "THOBE_RETURN": "REGISTRY",
    "MONEY_RETURNED": "REGISTRY",
    "LUNCH": "LUNCH",
}

# "Can do" per role, written out by hand from the redesign.
SPEC_ACTIVITY_PAGES = {
    "REGISTRY": {"REGISTRATION", "THOBE_ALLOCATION", "MONEY_RECEIVED", "THOBE_RETURN", "MONEY_RETURNED"},
    "SEATING": {"SEATING"},
    "QUEUE": {"QUEUE"},
    "STAGE": {"STAGE"},
    "LUNCH": {"LUNCH"},
    "CALLER": set(),                 # read-only: no activity at all
    "ADMIN": set(ACTIVITIES),        # "All seven pages"
    "DEPUTY_ADMIN": set(ACTIVITIES),  # "identical powers"
}
# The Registry desk screen (/station/registry): the Registry operator and the Admins.
SPEC_REGISTRY_DESK = {"REGISTRY", "ADMIN", "DEPUTY_ADMIN"}
SPEC_ADMIN_CONSOLE = {"ADMIN", "DEPUTY_ADMIN"}  # dashboard, corrections, users ... Admin only


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
    """Nine accounts, one per identity. Any of them can sign in from any browser."""
    w = SimpleNamespace(user_ids={})
    with engine.begin() as c:
        for key, role in IDENTITIES.items():
            w.user_ids[key] = users_svc.create_user(
                c, username=f"{key}-user", password=PASSWORD, role=role, full_name=key.replace("_", " ").title())
    return w


def build_app():
    return create_app(settings=Settings(database_url=TEST_DB_URL))


@pytest.fixture(scope="module")
def apps(engine, world):
    app = build_app()

    @app.post("/_probe/{activity}", dependencies=[Depends(require_user)])
    def probe(activity: str):
        return {"ok": True}

    return app


def new_client(app):
    return TestClient(app)


def api_login(client, username, password=PASSWORD, **extra):
    return client.post("/api/login", json={"username": username, "password": password, **extra})


_CLIENTS = {}


def signed_in(apps, world, key):
    """A cached, logged-in client for identity `key`."""
    if key not in _CLIENTS:
        client = new_client(apps)
        response = api_login(client, f"{key}-user")
        assert response.status_code == 200, response.text
        _CLIENTS[key] = client
    return _CLIENTS[key]


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
        assert len(permissions.ALL_ROLES) == 8

    @pytest.mark.parametrize("role", RETIRED_ROLES)
    def test_the_three_merged_roles_no_longer_exist(self, role):
        assert role not in permissions.ALL_ROLES
        assert permissions.permissions_for(role) == frozenset()

    def test_admin_and_deputy_have_exactly_the_same_permissions(self):
        assert permissions.permissions_for("ADMIN") == permissions.permissions_for("DEPUTY_ADMIN")

    @pytest.mark.parametrize("role", sorted(OPERATOR_ROLES))
    def test_an_operator_has_only_their_own_activities_and_no_admin_power(self, role):
        perms = permissions.permissions_for(role)
        extra = {CALLER_VIEW} if role in SPEC_CALLER_SCREEN else set()
        assert perms == {permissions.activity_permission(a) for a in SPEC_ACTIVITY_PAGES[role]} | extra
        assert not permissions.has_permission(role, permissions.ADMIN_PERMISSION)

    @pytest.mark.parametrize("role", sorted(IDENTITIES.values()))
    def test_only_the_caller_stage_and_admins_may_view_the_caller_screen(self, role):
        assert permissions.has_permission(role, CALLER_VIEW) == (role in SPEC_CALLER_SCREEN)

    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_every_activity_has_exactly_the_operator_role_the_redesign_names(self, activity):
        assert permissions.operator_role_for(activity) == OPERATOR_ROLE_FOR[activity]

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
        client = new_client(apps)
        wrong_pw = api_login(client, "admin-user", "Definitely-wrong-1")
        no_user = api_login(client, "nobody-here", PASSWORD)
        assert wrong_pw.status_code == no_user.status_code == 401
        assert detail_code(wrong_pw) == detail_code(no_user) == "BAD_CREDENTIALS"
        assert wrong_pw.json() == no_user.json()  # cannot tell which usernames exist

    def test_login_is_case_insensitive_on_username(self, apps, world):
        assert api_login(new_client(apps), "ADMIN-USER").status_code == 200

    def test_disabled_user_cannot_log_in_even_with_the_correct_password(self, apps, engine, world):
        username, _ = fresh_user(engine, role="ADMIN", active=False)
        response = api_login(new_client(apps), username, PASSWORD)
        assert response.status_code == 403
        assert detail_code(response) == "ACCOUNT_DISABLED"

    def test_disabled_operator_cannot_log_in(self, apps, engine, world):
        username, _ = fresh_user(engine, role="LUNCH", active=False)
        response = api_login(new_client(apps), username)
        assert response.status_code == 403 and detail_code(response) == "ACCOUNT_DISABLED"

    def test_a_disabled_account_does_not_reveal_itself_to_a_wrong_password(self, apps, engine, world):
        username, _ = fresh_user(engine, role="ADMIN", active=False)
        response = api_login(new_client(apps), username, "Definitely-wrong-1")
        assert response.status_code == 401 and detail_code(response) == "BAD_CREDENTIALS"

    def test_disabling_a_user_ends_their_existing_session_immediately(self, apps, engine, world):
        username, uid = fresh_user(engine, role="LUNCH")
        client = new_client(apps)
        assert api_login(client, username).status_code == 200
        assert client.get("/api/me").status_code == 200
        with engine.begin() as c:
            users_svc.set_user_active(c, uid, False, actor_id=world.user_ids["admin"])
        assert client.get("/api/me").status_code == 401
        with engine.begin() as c:
            users_svc.set_user_active(c, uid, True, actor_id=world.user_ids["admin"])
        assert api_login(new_client(apps), username).status_code == 200

    def test_a_switched_off_account_is_refused_even_if_no_one_revoked_its_session(self, apps, engine, world):
        # Second layer: validity is re-checked against the user row on every request, so a direct
        # database change (or a code path that forgets to revoke) still cuts the session off.
        username, uid = fresh_user(engine, role="LUNCH")
        client = new_client(apps)
        api_login(client, username)
        assert client.get("/api/me").status_code == 200
        with engine.begin() as c:
            c.execute(text("UPDATE users SET active = false WHERE id = :i"), {"i": uid})
        assert client.get("/api/me").status_code == 401

    def test_login_reports_who_but_never_the_password_hash(self, apps, world):
        response = api_login(new_client(apps), "registry-user")
        body = response.json()
        assert body["user"]["role"] == "REGISTRY"
        assert "password" not in response.text.lower() and "argon2" not in response.text

    def test_the_browser_form_login_sets_a_cookie_and_redirects(self, apps, world):
        client = new_client(apps)
        response = client.post("/login", data={"username": "registry-user", "password": PASSWORD},
                               follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/station/registry"
        assert SESSION_COOKIE in client.cookies
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=strict" in set_cookie

    def test_the_form_login_page_and_failed_form_login_show_a_plain_sentence(self, apps):
        client = new_client(apps)
        assert client.get("/login").status_code == 200
        failed = client.post("/login", data={"username": "admin-user", "password": "nope-nope-nope"})
        assert failed.status_code == 401 and "Wrong username or password." in failed.text

    def test_admin_lands_on_the_admin_console_and_root_redirects_by_role(self, apps, world):
        admin = signed_in(apps, world, "admin")
        assert admin.get("/", follow_redirects=False).headers["location"] == "/admin"
        operator = signed_in(apps, world, "seating")
        assert operator.get("/", follow_redirects=False).headers["location"] == "/station/seating"
        assert new_client(apps).get("/", follow_redirects=False).headers["location"] == "/login"


class TestSessions:
    def _login(self, apps, world, engine, role="LUNCH"):
        username, _ = fresh_user(engine, role=role)
        client = new_client(apps)
        response = api_login(client, username)
        assert response.status_code == 200
        return client, response.json()["token"]

    def test_documented_timeouts_are_the_defaults(self):
        settings = Settings(database_url=TEST_DB_URL)
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
        replay = new_client(apps)
        assert replay.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_bearer_token_works_like_the_cookie(self, apps, world, engine):
        _, token = self._login(apps, world, engine)
        other = new_client(apps)
        response = other.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200 and response.json()["role"] == "LUNCH"

    def test_garbage_tokens_are_rejected_plainly(self, apps):
        client = new_client(apps)
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
# The role matrix: 9 identities x every protected endpoint
# --------------------------------------------------------------------------- #
STATION_PAGES = [("GET", f"/station/{slug(a)}") for a in ACTIVITIES] + [("GET", "/station/registry")]
CALLER_PAGES = [("GET", "/caller"), ("GET", "/caller/state"), ("GET", "/caller/queue"), ("GET", "/caller/queue-events")]
ADMIN_ENDPOINTS = [
    ("GET", "/admin"), ("GET", "/admin/users"),
    ("POST", "/admin/users"),
    ("POST", "/admin/import/preview"), ("POST", "/admin/import/commit"),
    ("POST", "/admin/photos/link"), ("POST", "/admin/master-pack/import"),
]
ENDPOINTS = [("GET", "/api/me")] + STATION_PAGES + CALLER_PAGES + ADMIN_ENDPOINTS


def expected_allowed(role, path):
    """Spec section 4, independent of the implementation."""
    if path == "/api/me":
        return True
    if path == "/station/registry":
        return role in SPEC_REGISTRY_DESK
    if path.startswith("/caller"):
        return role in SPEC_CALLER_SCREEN
    if path.startswith("/station/"):
        activity = next(a for a in ACTIVITIES if slug(a) == path.rsplit("/", 1)[1])
        return activity in SPEC_ACTIVITY_PAGES[role]
    return role in SPEC_ADMIN_CONSOLE


MATRIX = [
    pytest.param(key, method, path, id=f"{key}-{method}-{path}")
    for key in IDENTITIES for method, path in ENDPOINTS
]


class TestRoleMatrix:
    @pytest.mark.parametrize("key,method,path", MATRIX)
    def test_each_role_reaches_exactly_its_own_endpoints(self, apps, world, key, method, path):
        client = signed_in(apps, world, key)
        response = client.request(method, path)
        role = IDENTITIES[key]
        if expected_allowed(role, path):
            assert response.status_code not in (401, 403), (response.status_code, response.text[:200])
        else:
            assert response.status_code == 403, (response.status_code, response.text[:200])

    @pytest.mark.parametrize("method,path", ENDPOINTS[1:])
    def test_anonymous_callers_get_401_everywhere(self, apps, method, path):
        assert new_client(apps).request(method, path).status_code == 401

    def test_admin_and_deputy_get_identical_answers_to_every_request(self, apps, world):
        statuses = {}
        for key in ("admin", "deputy"):
            client = signed_in(apps, world, key)
            statuses[key] = [client.request(m, p).status_code for m, p in ENDPOINTS]
        assert statuses["admin"] == statuses["deputy"]
        assert all(s not in (401, 403) for s in statuses["admin"])  # admin/deputy reach everything

    def test_a_registry_operator_reaches_only_the_registry_desk_and_its_three_activities(self, apps, world):
        client = signed_in(apps, world, "registry")
        assert client.get("/station/registry").status_code == 200
        for activity in ACTIVITIES:
            expected = 200 if activity in SPEC_ACTIVITY_PAGES["REGISTRY"] else 403
            assert client.get(f"/station/{slug(activity)}").status_code == expected, activity
        for _, path in ADMIN_ENDPOINTS:
            assert client.get(path).status_code in (403, 405)

    def test_a_caller_lands_on_the_caller_screen(self, apps, world):
        client = new_client(apps)
        response = client.post("/login", data={"username": "caller-user", "password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/caller"

    def test_a_registry_operator_lands_on_the_registry_desk(self, apps, world):
        client = new_client(apps)
        response = client.post("/login", data={"username": "registry-user", "password": PASSWORD}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/station/registry"

    @pytest.mark.parametrize("key", [k for k, r in IDENTITIES.items() if r in OPERATOR_ROLES])
    def test_operators_are_locked_out_of_the_phase_3_admin_endpoints(self, apps, world, key):
        client = signed_in(apps, world, key)
        assert client.post("/admin/snapshot/freeze").status_code == 403
        assert client.get("/admin/master-pack/export").status_code == 403
        assert client.post("/admin/import/commit").status_code == 403

    @pytest.mark.parametrize("key", ["admin", "deputy"])
    def test_admin_and_deputy_can_use_the_phase_3_endpoints(self, apps, world, key):
        client = signed_in(apps, world, key)
        assert client.post("/admin/snapshot/freeze").status_code == 200
        assert client.post("/admin/photos/link").status_code == 422  # reached the handler: form is empty

    def test_every_route_is_protected_unless_deliberately_public(self, engine, world):
        app = build_app()
        public = {("GET", "/"), ("GET", "/login"), ("POST", "/login"), ("POST", "/api/login"), ("GET", "/health"),
                  # Phase 11: the audience screen. Deliberately public; serves only the approved LED payload.
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
        assert {("GET", "/api/me"), ("GET", "/station/{activity}"),
                ("POST", "/admin/users/{user_id}/active"), ("POST", "/admin/snapshot/freeze"),
                ("GET", "/admin/master-pack/export"), ("POST", "/logout")} <= visited
        assert len(visited) >= 15


# --------------------------------------------------------------------------- #
# Admin screens: user management
# --------------------------------------------------------------------------- #
class TestUserManagement:
    def _admin(self, apps, key="admin"):
        client = new_client(apps)
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
        assert api_login(new_client(apps), username, "lunch-desk-1").status_code == 200

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
        assert detail_code(api_login(new_client(apps), "admin-user")) == "ACCOUNT_DISABLED"
        deputy.post(f"/admin/users/{world.user_ids['admin']}/active", data={"active": "1"}, follow_redirects=False)
        assert api_login(new_client(apps), "admin-user").status_code == 200

    def test_password_reset_replaces_the_password_and_ends_old_sessions(self, apps, world, engine):
        username, uid = fresh_user(engine, role="LUNCH")
        laptop = new_client(apps)
        api_login(laptop, username)
        response = self._admin(apps).post(f"/admin/users/{uid}/password", data={"password": "brand-new-pass-1"}, follow_redirects=False)
        assert "msg" in redirect_query(response)
        assert laptop.get("/api/me").status_code == 401
        assert api_login(new_client(apps), username, PASSWORD).status_code == 401
        assert api_login(new_client(apps), username, "brand-new-pass-1").status_code == 200

    def test_user_actions_are_audited_under_the_individuals_own_login(self, apps, world, engine):
        username, uid = fresh_user(engine, role="STAGE")
        self._admin(apps, "deputy").post(f"/admin/users/{uid}/active", data={"active": "0"}, follow_redirects=False)
        with engine.connect() as c:
            row = c.execute(text("SELECT operator_id, details FROM audit_log WHERE action='USER_DEACTIVATED' "
                                 "AND details->>'username'=:u"), {"u": username}).one()
        assert row.operator_id == world.user_ids["deputy"]

    def test_admin_can_delete_a_user(self, apps, world, engine):
        username = f"del-{uuid.uuid4().hex[:6]}"
        client = self._admin(apps)
        client.post("/admin/users", data={"username": username, "full_name": "", "role": "LUNCH", "password": "x"*12})
        row = self._row(engine, username)
        assert row is not None

        response = client.post(f"/admin/users/{row['id']}/delete", follow_redirects=False)
        assert "msg" in redirect_query(response)
        assert self._row(engine, username) is None

        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM audit_log WHERE action='USER_DELETED' AND details->>'username'=:u"), {"u": username}).scalar() == 1

    def test_deleting_self_is_refused(self, apps, world, engine):
        client = self._admin(apps)
        response = client.post(f"/admin/users/{world.user_ids['admin']}/delete", follow_redirects=False)
        assert "error" in redirect_query(response)

    def test_deleting_last_admin_is_refused(self, apps, world, engine):
        from backend import users
        from backend.users import AccountError
        conn = engine.connect()
        trans = conn.begin()
        try:
            # Change all other admins to LUNCH so deputy is the last admin
            conn.execute(text("UPDATE users SET role='LUNCH' WHERE id != :i"), {"i": world.user_ids["deputy"]})
            with pytest.raises(AccountError) as exc:
                users.delete_user(conn, world.user_ids["deputy"], actor_id=world.user_ids["admin"])
            assert exc.value.code == "LAST_ADMIN"
        finally:
            trans.rollback()
            conn.close()

    def test_deleting_the_last_active_admin_is_refused_even_if_an_inactive_admin_row_exists(self, engine, world):
        from backend import users
        from backend.users import AccountError
        conn = engine.connect()
        trans = conn.begin()
        try:
            extra_id = users.create_user(conn, username=f"extra-admin-{uuid.uuid4().hex[:6]}", password=PASSWORD,
                                          role="DEPUTY_ADMIN", actor_id=world.user_ids["admin"])
            users.set_user_active(conn, extra_id, False, actor_id=world.user_ids["admin"])
            users.set_user_active(conn, world.user_ids["deputy"], False, actor_id=world.user_ids["admin"])
            # admin is now the only ACTIVE admin/deputy account: both the deputy and the freshly
            # created extra admin are inactive rows that must not count toward "one remains".
            with pytest.raises(AccountError) as exc:
                users.delete_user(conn, world.user_ids["admin"], actor_id=extra_id)
            assert exc.value.code == "LAST_ADMIN"
        finally:
            trans.rollback()
            conn.close()

    def test_deleting_an_inactive_admin_does_not_require_another_admin_to_remain(self, engine, world):
        from backend import users
        conn = engine.connect()
        trans = conn.begin()
        try:
            extra_id = users.create_user(conn, username=f"extra-admin-{uuid.uuid4().hex[:6]}", password=PASSWORD,
                                          role="DEPUTY_ADMIN", actor_id=world.user_ids["admin"])
            users.set_user_active(conn, extra_id, False, actor_id=world.user_ids["admin"])
            # This inactive account isn't protecting anything: deleting it must not be blocked by
            # the LAST_ADMIN guard just because it's still the only other admin/deputy row.
            users.delete_user(conn, extra_id, actor_id=world.user_ids["admin"])
            assert conn.execute(text("SELECT count(*) FROM users WHERE id = :i"), {"i": extra_id}).scalar() == 0
        finally:
            trans.rollback()
            conn.close()

    def test_the_user_list_never_shows_password_hashes(self, apps, world):
        page = self._admin(apps).get("/admin/users")
        assert page.status_code == 200 and "argon2" not in page.text and "lunch-user" in page.text


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
        assert api_login(new_client(apps), f"admin.{tag}", "Admin-Seed-Pass-1").status_code == 200
        assert api_login(new_client(apps), f"deputy.{tag}", "Deputy-Seed-Pass-1").status_code == 200

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
        env = {**os.environ, **self._env(tag), "DATABASE_URL": TEST_DB_URL}
        run = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "seed_admins.py")], cwd=REPO_ROOT, env=env,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Admin-Seed-Pass-1" not in run.stdout + run.stderr
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM users WHERE username IN (:a,:d)"), {"a": f"admin.{tag}", "d": f"deputy.{tag}"}).scalar_one() == 2

    def test_the_command_line_script_fails_cleanly_when_it_cannot_prompt(self, engine):
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("SEED_")}
        env.update({"DATABASE_URL": TEST_DB_URL})
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

    @pytest.mark.parametrize("role", RETIRED_ROLES)
    def test_the_database_refuses_the_three_merged_roles(self, conn, role):
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('old-x', 'h', :r)"), {"r": role})

    def test_session_tokens_are_unique_and_tied_to_a_real_user(self, conn, world):
        conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h1', :u, now() + interval '1 hour')"),
                     {"u": world.user_ids["admin"]})
        with db_error(conn, UNIQUE_VIOLATION):
            conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h1', :u, now() + interval '1 hour')"),
                         {"u": world.user_ids["admin"]})
        with db_error(conn, "23503"):
            conn.execute(text("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ('h2', gen_random_uuid(), now() + interval '1 hour')"))


class TestUserDeactivation:
    def test_deactivated_user_cannot_log_in(self, engine, apps, world):
        from backend import users
        with engine.begin() as conn:
            op_id = users.create_user(conn, username="reg-op", password=PASSWORD, role="REGISTRY", actor_id=world.user_ids["admin"])
            users.set_user_active(conn, op_id, False, actor_id=world.user_ids["admin"])
        client = new_client(apps)
        response = api_login(client, "reg-op")
        assert response.status_code == 403
        assert detail_code(response) == "ACCOUNT_DISABLED"

    def test_cannot_deactivate_last_admin(self, engine, world):
        from backend import users
        from backend.users import AccountError
        with engine.begin() as conn:
            # Set ALL admins except the main admin to inactive
            conn.execute(text("UPDATE users SET active=False WHERE role IN ('ADMIN', 'DEPUTY_ADMIN') AND id != :i"), {"i": world.user_ids["admin"]})

            # Cannot deactivate the original admin since it's the last one
            with pytest.raises(AccountError) as exc:
                users.set_user_active(conn, world.user_ids["admin"], False, actor_id=world.user_ids["deputy"])
            assert exc.value.code == "LAST_ADMIN"

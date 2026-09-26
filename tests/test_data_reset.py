"""Admin → System → Reset all data (backend/admin/reset.py), against the real schema built by `alembic upgrade head`.

What is proved here:
  * only a signed-in ADMIN / DEPUTY_ADMIN reaches any of it; an operator gets 403, a visitor 401;
  * the exact phrase AND the Admin's own password are both required, a wrong password is audited and five of them
    lock the form, and only a fresh, unused confirmation issued to the same Admin runs the reset;
  * the reset empties every event/import table in ONE transaction, keeps accounts, sessions, settings and the
    sign-in/account/reset audit rows, and leaves every append-only guard trigger switched back on;
  * a failure inside the database step deletes nothing at all (and touches no photo);
  * the photo clean-up removes only THIS app's Cloudinary assets, reports a failure as a failure (never
    "success"), can be retried until it finishes, and a retry never removes a photo a new student uses;
  * the reset and an import commit cannot overlap;
  * every reset stays on record, even across a second reset.
No real Cloudinary account is used, and the repository's own photos/ folder is never touched.
"""
import hashlib
import re
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from backend import photo_storage
from backend import users as users_svc
from backend.admin import reset as reset_svc
from backend.photo_storage import DEFAULT_LOCAL_ROOT, CloudinaryPhotoStore, LocalPhotoStore
from tests.admin_support import add_event, rows, scalar
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_combined_import import a_set_of_students, import_both, photo_zip, student_sheet
from tests.test_photo_storage import API_KEY, SECRET, FakeCloudinary, jpeg_bytes
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    world,
)

PHRASE = reset_svc.CONFIRM_PHRASE
FOLDER = "convocation/student-photos"


# --------------------------------------------------------------------------- fixtures
def _repo_photos():
    return sorted(p.name for p in DEFAULT_LOCAL_ROOT.iterdir()) if DEFAULT_LOCAL_ROOT.is_dir() else []


@pytest.fixture(scope="module", autouse=True)
def _repository_photos_are_never_touched():
    before = _repo_photos()
    yield
    assert _repo_photos() == before, "a reset test reached the repository's own photos/ folder"


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(autouse=True)
def sandbox(apps, tmp_path):
    """Every test gets its own photo folder and staging folder: a reset must never reach real ones."""
    original_store, original_staging = apps.state.photo_store, apps.state.settings.import_staging_dir
    local = LocalPhotoStore(tmp_path / "photos")
    apps.state.photo_store = local
    photo_storage.set_current_store(local)
    apps.state.settings.import_staging_dir = str(tmp_path / "staging")
    yield local
    apps.state.photo_store = original_store
    photo_storage.set_current_store(original_store)
    apps.state.settings.import_staging_dir = original_staging


def use_store(apps, store):
    apps.state.photo_store = store
    photo_storage.set_current_store(store)
    return store


class FakeCloudinaryAdmin(FakeCloudinary):
    """FakeCloudinary plus the two Admin API calls the purge uses. The listing filters by prefix exactly as
    Cloudinary does, pages with a cursor, and can be told to fail; so can the delete."""

    def __init__(self, *, list_fails=None, delete_fails=None, **kw):
        super().__init__(**kw)
        self.list_calls, self.delete_calls = [], []
        self.list_fails, self.delete_fails = list_fails, delete_fails

    def lister(self, **options):
        self.list_calls.append({k: v for k, v in options.items() if k not in ("api_key", "api_secret")})
        if self.list_fails:
            raise self.list_fails
        assert options["type"] == "authenticated" and options["resource_type"] == "image"
        assert options["api_secret"] == SECRET and options["api_key"] == API_KEY
        ids = sorted(p for p in self.assets if p.startswith(options["prefix"]))
        start = int(options.get("next_cursor") or 0)
        page = ids[start:start + options["max_results"]]
        end = start + len(page)
        return {"resources": [{"public_id": p} for p in page], **({"next_cursor": str(end)} if end < len(ids) else {})}

    def deleter(self, public_ids, **options):
        self.delete_calls.append(list(public_ids))
        if self.delete_fails:
            raise self.delete_fails
        assert options["type"] == "authenticated" and options["resource_type"] == "image"
        return {"deleted": {p: ("deleted" if self.assets.pop(p, None) is not None else "not_found") for p in public_ids}}

    def store(self, **kw) -> CloudinaryPhotoStore:
        return CloudinaryPhotoStore(cloud_name="demo-cloud", api_key=API_KEY, api_secret=SECRET, folder=FOLDER,
                                    uploader=self.uploader, url_builder=self.url_builder, fetcher=self.fetcher,
                                    lister=self.lister, deleter=self.deleter, **kw)

    def own(self):
        return {p for p in self.assets if re.fullmatch(rf"{FOLDER}/[0-9a-f]{{32}}", p)}


FOREIGN = {
    "other-app/portraits/0123456789abcdef0123456789abcdef": b"someone else's",
    f"{FOLDER}-archive/0123456789abcdef0123456789abcdef": b"a look-alike folder",
    f"{FOLDER}/manual-upload": b"not our naming scheme",
    f"{FOLDER}/sub/0123456789abcdef0123456789abcdef": b"a sub-folder of ours",
    "convocation/0123456789abcdef0123456789abcdef": b"the parent folder",
}


# --------------------------------------------------------------------------- helpers
def location(response):
    assert response.status_code == 303, (response.status_code, response.text[:300])
    parsed = urlparse(response.headers["location"])
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def ask(client, phrase=PHRASE, password=PASSWORD):
    return client.post("/admin/system/reset", data={"phrase": phrase, "password": password}, follow_redirects=False)


def confirmation_of(response):
    assert response.status_code == 200, (response.status_code, response.text[:300])
    found = re.search(r'name="confirmation" value="([^"]+)"', response.text)
    assert found, response.text[:500]
    return found.group(1)


def execute(client, token):
    return client.post("/admin/system/reset/execute", data={"confirmation": token}, follow_redirects=False)


def full_reset(client):
    return execute(client, confirmation_of(ask(client)))


def admin_id(engine):
    return scalar(engine, "SELECT id FROM users WHERE username = 'eng-admin'")


def audit_count(engine, action):
    return scalar(engine, "SELECT count(*) FROM audit_log WHERE action = :a", a=action)


DATA_TABLES = ("students", "qr_tokens", "activity_events", "scan_log", "queue", "exceptions", "display_snapshot", "counters")


def data_counts(engine):
    return {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in DATA_TABLES}


def populate(engine):
    """A little of everything a live event leaves behind, written as raw rows."""
    people = [make_student(engine, photo_path=f"photos/p{i}.jpg") for i in range(3)]
    a, b, c = people
    for activity in ("REGISTRATION", "THOBE_ALLOCATION", "SEATING", "QUEUE"):
        add_event(engine, a, activity)
    reg = add_event(engine, b, "REGISTRATION")
    add_event(engine, b, "REGISTRATION", kind="REVERSAL", corrects=reg, details={"reason": "wrong student"})
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO queue (student_id, status) VALUES (:s, 'DISPLAYED')"), {"s": a.id})
        conn.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school, photo_path) "
                          "VALUES (:s, 'A', 'B.Tech', 'Eng', 'photos/p0.jpg')"), {"s": a.id})
        conn.execute(text("INSERT INTO scan_log (activity, result, student_id) VALUES ('SEATING', 'SUCCESS', :s)"), {"s": a.id})
        conn.execute(text("INSERT INTO exceptions (type, student_id) VALUES ('RETURN_WAIVED', :s)"), {"s": c.id})
        conn.execute(text("INSERT INTO audit_log (action, student_id) VALUES ('QR_REISSUED', :s)"), {"s": a.id})
        conn.execute(text("INSERT INTO audit_log (action, details) VALUES ('IMPORT_STUDENTS', '{}'::jsonb)"))
        conn.execute(text("INSERT INTO audit_log (action, details) VALUES ('EXPORT', '{}'::jsonb)"))
    return people


# ══════════════════════════════════════════════════════════════════ who may reset
class TestAccess:
    ENDPOINTS = [("GET", "/admin/system"), ("POST", "/admin/system/reset"),
                 ("POST", "/admin/system/reset/execute"), ("POST", "/admin/system/reset/photo-cleanup")]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_a_signed_out_visitor_is_refused(self, apps, method, path):
        response = new_client(apps).request(method, path, follow_redirects=False)
        assert response.status_code == 401

    @pytest.mark.parametrize("activity", ["REGISTRATION", "SEATING", "LUNCH"])
    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_an_operator_is_refused_and_nothing_is_deleted(self, apps, world, engine, activity, method, path):
        make_student(engine)
        before = data_counts(engine)
        response = operator(apps, world, activity).request(
            method, path, data={"phrase": PHRASE, "password": PASSWORD, "confirmation": "x"}, follow_redirects=False)
        assert response.status_code == 403
        assert data_counts(engine) == before

    def test_the_deputy_admin_can_open_it(self, apps, engine):
        username = f"deputy-{uuid.uuid4().hex[:6]}"
        with engine.begin() as conn:
            users_svc.create_user(conn, username=username, password=PASSWORD, role="DEPUTY_ADMIN")
        client = new_client(apps)
        assert api_login(client, username).status_code == 200
        assert client.get("/admin/system").status_code == 200

    def test_the_admin_home_links_to_the_system_page(self, apps):
        page = admin(apps).get("/admin").text
        assert 'href="/admin/system"' in page


# ══════════════════════════════════════════════════════════════════ not one click
class TestConfirmation:
    def test_the_page_explains_what_goes_and_what_stays(self, apps, engine):
        make_student(engine)
        page = admin(apps).get("/admin/system").text
        assert "cannot be undone" in page and PHRASE in page and 'type="password"' in page
        for words in ("Students", "QR codes", "Activity records", "Caller display", "photo storage", "Kept:", "user accounts"):
            assert words in page

    @pytest.mark.parametrize("phrase", ["delete all data", "DELETE ALL", "DELETE  ALL DATA", "", "DELETE ALL DATA!",
                                        "yes"])
    def test_anything_but_the_exact_phrase_is_refused(self, apps, engine, phrase):
        make_student(engine)
        before, issued = data_counts(engine), audit_count(engine, "DATA_RESET_REQUESTED")
        path, query = location(ask(admin(apps), phrase=phrase))
        assert path == "/admin/system" and PHRASE in query["error"]
        assert data_counts(engine) == before
        assert audit_count(engine, "DATA_RESET_REQUESTED") == issued          # no confirmation was issued

    def test_a_wrong_password_is_refused_and_recorded(self, apps, engine):
        make_student(engine)
        before, refused = data_counts(engine), audit_count(engine, "DATA_RESET_REFUSED")
        path, query = location(ask(admin(apps), password="not-the-password"))
        assert query["error"] == "That password is not correct. Nothing was deleted."
        assert data_counts(engine) == before
        assert audit_count(engine, "DATA_RESET_REFUSED") == refused + 1

    def test_five_wrong_passwords_lock_the_form_even_for_the_right_one(self, apps, engine):
        username = f"admin-{uuid.uuid4().hex[:6]}"
        with engine.begin() as conn:
            users_svc.create_user(conn, username=username, password=PASSWORD, role="ADMIN")
        client = new_client(apps)
        assert api_login(client, username).status_code == 200
        for _ in range(reset_svc.MAX_REFUSALS):
            ask(client, password="wrong-password-123")
        _, query = location(ask(client))
        assert "Too many wrong passwords" in query["error"]

    def test_phrase_and_password_only_lead_to_a_final_confirmation_page(self, apps, engine):
        make_student(engine)
        before = data_counts(engine)
        response = ask(admin(apps))
        token = confirmation_of(response)
        assert "Final confirmation" in response.text and "Yes, delete all data" in response.text
        assert str(before["students"]) in response.text                    # the exact count is shown
        assert data_counts(engine) == before                               # nothing deleted yet
        stored = rows(engine, "SELECT details FROM audit_log WHERE action = 'DATA_RESET_REQUESTED' ORDER BY id DESC LIMIT 1")[0]
        assert stored["details"]["confirmation"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(stored)                                    # only the hash is kept

    @pytest.mark.parametrize("token", ["", "made-up", "x" * 43])
    def test_a_made_up_confirmation_deletes_nothing(self, apps, engine, token):
        make_student(engine)
        before = data_counts(engine)
        _, query = location(execute(admin(apps), token))
        assert "expired" in query["error"]
        assert data_counts(engine) == before

    def test_a_confirmation_issued_to_another_admin_cannot_be_used(self, apps, engine):
        username = f"admin-{uuid.uuid4().hex[:6]}"
        with engine.begin() as conn:
            users_svc.create_user(conn, username=username, password=PASSWORD, role="ADMIN")
        other = new_client(apps)
        assert api_login(other, username).status_code == 200
        token = confirmation_of(ask(other))
        make_student(engine)
        before = data_counts(engine)
        _, query = location(execute(admin(apps), token))
        assert "expired" in query["error"] and data_counts(engine) == before

    def test_an_expired_confirmation_is_refused(self, apps, engine):
        token = "an-old-confirmation-token"
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO audit_log (action, operator_id, details, occurred_at) VALUES ('DATA_RESET_REQUESTED', :u, "
                "jsonb_build_object('confirmation', CAST(:h AS text)), now() - make_interval(mins => :m))"),
                {"u": admin_id(engine), "h": hashlib.sha256(token.encode()).hexdigest(), "m": reset_svc.CONFIRM_TTL_MINUTES + 1})
        make_student(engine)
        before = data_counts(engine)
        _, query = location(execute(admin(apps), token))
        assert "expired" in query["error"] and data_counts(engine) == before

    def test_a_confirmation_works_only_once(self, apps, engine):
        token = confirmation_of(ask(admin(apps)))
        _, first = location(execute(admin(apps), token))
        assert "msg" in first
        make_student(engine)
        _, second = location(execute(admin(apps), token))
        assert "already been used" in second["error"]
        assert scalar(engine, "SELECT count(*) FROM students") == 1


# ══════════════════════════════════════════════════════════════════ what the reset does to the database
class TestDatabase:
    def test_every_table_is_classified(self, engine):
        """A new table has to be put in one of the three lists before a reset can be trusted with it."""
        tables = {r["tablename"] for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        classified = set(reset_svc.CLEARED_TABLES) | set(reset_svc.PARTLY_CLEARED_TABLES) | set(reset_svc.KEPT_TABLES)
        assert tables == classified

    def test_the_reset_deletes_all_event_and_import_data(self, apps, engine, sandbox):
        populate(engine)
        assert all(data_counts(engine)[t] > 0 for t in DATA_TABLES)
        path, query = location(full_reset(admin(apps)))
        assert path == "/admin/system" and query["msg"].startswith("All data was deleted")
        assert data_counts(engine) == dict.fromkeys(DATA_TABLES, 0)
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE student_id IS NOT NULL") == 0
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE action IN ('QR_REISSUED','IMPORT_STUDENTS','EXPORT')") == 0
        assert scalar(engine, "SELECT count(*) FROM student_status") == 0

    def test_the_queue_numbers_start_again_at_one(self, apps, engine):
        populate(engine)
        location(full_reset(admin(apps)))
        s = make_student(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s.id})
        assert scalar(engine, "SELECT queue_position FROM queue WHERE student_id = :s", s=s.id) == 1

    def test_accounts_sessions_settings_and_sign_in_history_are_kept(self, apps, engine, world):
        populate(engine)
        with engine.begin() as conn:
            conn.execute(text("UPDATE settings SET event_name = 'Kept Event', holding_screen_text = 'Welcome'"))
        users_before = rows(engine, "SELECT id, username, password_hash, role, active FROM users ORDER BY username")
        sessions_before = scalar(engine, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL")
        kept_audit = rows(engine, "SELECT id FROM audit_log WHERE action IN ('LOGIN', 'USER_CREATED') ORDER BY id")
        revision = scalar(engine, "SELECT version_num FROM alembic_version")
        assert kept_audit and users_before

        location(full_reset(admin(apps)))

        assert rows(engine, "SELECT id, username, password_hash, role, active FROM users ORDER BY username") == users_before
        assert scalar(engine, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == sessions_before
        assert rows(engine, "SELECT event_name, holding_screen_text FROM settings") == [
            {"event_name": "Kept Event", "holding_screen_text": "Welcome"}]
        assert rows(engine, "SELECT id FROM audit_log WHERE action IN ('LOGIN', 'USER_CREATED') ORDER BY id") == kept_audit
        assert scalar(engine, "SELECT version_num FROM alembic_version") == revision
        assert admin(apps).get("/admin").status_code == 200                # still signed in
        assert api_login(new_client(apps), "eng-lunch").status_code == 200  # operators can still sign in

    def test_every_guard_trigger_is_back_on_and_history_is_append_only_again(self, apps, engine):
        populate(engine)
        location(full_reset(admin(apps)))
        states = {r["tgname"]: r["tgenabled"] for r in rows(engine, "SELECT tgname, tgenabled FROM pg_trigger WHERE NOT tgisinternal")}
        for trigger in reset_svc.GUARD_TRIGGERS.values():
            assert states[trigger] == "O", trigger
        s = make_student(engine)
        event = add_event(engine, s, "REGISTRATION")
        with pytest.raises(Exception, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM activity_events WHERE event_id = :e"), {"e": event})
        with pytest.raises(Exception, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM audit_log"))

    def test_a_failure_inside_the_database_step_deletes_nothing_and_touches_no_photo(self, apps, engine, monkeypatch):
        populate(engine)
        fake = FakeCloudinaryAdmin()
        store = use_store(apps, fake.store())
        store.save("p0.jpg", jpeg_bytes())
        before = data_counts(engine)
        audit_before = scalar(engine, "SELECT count(*) FROM audit_log")
        resets, failures = audit_count(engine, "DATA_RESET"), audit_count(engine, "DATA_RESET_FAILED")
        real = reset_svc._delete_all

        def half_then_crash(conn):
            real(conn)                                          # everything deleted inside the transaction ...
            raise RuntimeError("connection lost")               # ... and then it fails before COMMIT

        monkeypatch.setattr(reset_svc, "_delete_all", half_then_crash)
        _, query = location(full_reset(admin(apps)))
        assert query["error"] == "The reset could not be completed, so nothing was deleted. Please try again."
        assert "connection lost" not in query["error"]
        assert data_counts(engine) == before
        assert audit_count(engine, "DATA_RESET") == resets                  # the rolled-back record went with it
        assert audit_count(engine, "DATA_RESET_FAILED") == failures + 1     # ... and the failure is on record
        assert scalar(engine, "SELECT count(*) FROM audit_log") == audit_before + 2   # + the request, + the failure
        assert fake.delete_calls == [] and fake.list_calls == [] and len(fake.own()) == 1
        states = {r["tgname"]: r["tgenabled"] for r in rows(engine, "SELECT tgname, tgenabled FROM pg_trigger WHERE NOT tgisinternal")}
        assert all(states[t] == "O" for t in reset_svc.GUARD_TRIGGERS.values())

    def test_the_whole_reset_can_simply_be_run_again(self, apps, engine):
        populate(engine)
        location(full_reset(admin(apps)))
        path, query = location(full_reset(admin(apps)))                    # an empty database: harmless
        assert query["msg"].startswith("All data was deleted (0 student(s))")
        assert data_counts(engine) == dict.fromkeys(DATA_TABLES, 0)

    def test_staged_import_batches_are_removed_but_nothing_else_in_the_folder(self, apps, engine, tmp_path):
        staging = tmp_path / "staging"
        batch = staging / uuid.uuid4().hex
        batch.mkdir(parents=True)
        (batch / "upload.csv").write_text("prn,name\n")
        unrelated = staging / "not-a-batch"
        unrelated.mkdir()
        location(full_reset(admin(apps)))
        assert not batch.exists() and unrelated.exists()


# ══════════════════════════════════════════════════════════════════ photo storage
class TestPhotoCleanup:
    def test_only_this_apps_cloudinary_assets_are_deleted(self, apps, engine):
        fake = FakeCloudinaryAdmin()
        store = use_store(apps, fake.store())
        people, entries = a_set_of_students()
        import_both(admin(apps), student_sheet(people), photo_zip(entries))  # the real import, into the fake cloud
        ours = fake.own()
        assert len(ours) == 4
        fake.assets.update(FOREIGN)

        _, query = location(full_reset(admin(apps)))

        assert fake.own() == set()
        assert set(fake.assets) == set(FOREIGN)                             # nothing else in the account moved
        assert all(set(call) <= ours for call in fake.delete_calls)         # every delete names one of ours
        assert all(c["prefix"] == FOLDER + "/" for c in fake.list_calls)
        assert query["msg"].endswith("4 photo(s) were removed from photo storage.")
        assert store.load("photos/whatever.jpg") is None

    def test_the_listing_is_paged_to_the_end(self, apps, engine):
        fake = FakeCloudinaryAdmin()
        store = use_store(apps, fake.store())
        store.LIST_PAGE = 2
        for i in range(5):
            store.save(f"p{i}.jpg", jpeg_bytes())
        location(full_reset(admin(apps)))
        assert fake.own() == set() and len(fake.list_calls) == 3

    def test_no_public_id_or_credential_reaches_the_browser_or_the_audit_log(self, apps, engine):
        fake = FakeCloudinaryAdmin(delete_fails=RuntimeError(f"boom {SECRET} {FOLDER}/abc"))
        store = use_store(apps, fake.store())
        store.save("p0.jpg", jpeg_bytes())
        public_id = next(iter(fake.own()))
        client = admin(apps)
        confirm_page = ask(client)
        response = execute(client, confirmation_of(confirm_page))
        page = client.get("/admin/system").text
        audit = str(rows(engine, "SELECT details FROM audit_log WHERE action LIKE 'DATA_RESET%'"))
        for text_ in (confirm_page.text, response.headers["location"], page, audit):
            for secret in (SECRET, API_KEY, public_id, public_id.rsplit("/", 1)[1]):
                assert secret not in text_

    def test_a_cloudinary_failure_is_reported_never_called_success(self, apps, engine):
        fake = FakeCloudinaryAdmin(delete_fails=type("RateLimited", (Exception,), {})("slow down"))
        store = use_store(apps, fake.store())
        populate(engine)
        for i in range(3):
            store.save(f"p{i}.jpg", jpeg_bytes())

        _, query = location(full_reset(admin(apps)))

        assert "msg" not in query
        assert query["error"] == ("All data was deleted (3 student(s)), but the photo clean-up did not finish: "
                                  "3 photo(s) could not be removed from photo storage. Please use Retry photo clean-up.")
        assert data_counts(engine)["students"] == 0                        # the database part did happen
        assert len(fake.own()) == 3
        cleanup = rows(engine, "SELECT details FROM audit_log WHERE action = 'DATA_RESET_PHOTO_CLEANUP' ORDER BY id DESC LIMIT 1")[0]
        assert cleanup["details"]["photos"]["complete"] is False and cleanup["details"]["photos"]["failed"] == 3
        page = admin(apps).get("/admin/system").text
        assert 'data-last-reset="failed"' in page and "Retry photo clean-up" in page
        assert "limiting requests" in page

    def test_a_listing_failure_is_reported_too(self, apps, engine):
        fake = FakeCloudinaryAdmin(list_fails=ConnectionError("down"))
        store = use_store(apps, fake.store())
        store.save("p0.jpg", jpeg_bytes())
        _, query = location(full_reset(admin(apps)))
        assert "photo clean-up did not finish" in query["error"] and "could not be reached" in query["error"]

    def test_the_photo_cleanup_can_be_retried_until_it_finishes(self, apps, engine):
        fake = FakeCloudinaryAdmin(delete_fails=ConnectionError("down"))
        store = use_store(apps, fake.store())
        for i in range(3):
            store.save(f"p{i}.jpg", jpeg_bytes())
        location(full_reset(admin(apps)))
        assert len(fake.own()) == 3

        # still failing: says so again
        _, query = location(admin(apps).post("/admin/system/reset/photo-cleanup", follow_redirects=False))
        assert query["error"].startswith("The photo clean-up did not finish")

        fake.delete_fails = None
        _, query = location(admin(apps).post("/admin/system/reset/photo-cleanup", follow_redirects=False))
        assert query["msg"] == "Photo clean-up finished. 3 photo(s) were removed."
        assert fake.own() == set()
        assert 'data-last-reset="complete"' in admin(apps).get("/admin/system").text

        _, query = location(admin(apps).post("/admin/system/reset/photo-cleanup", follow_redirects=False))
        assert query["error"] == "There is no unfinished photo clean-up."

    def test_a_retry_never_removes_a_photo_a_new_student_uses(self, apps, engine):
        fake = FakeCloudinaryAdmin(delete_fails=ConnectionError("down"))
        store = use_store(apps, fake.store())
        store.save("old.jpg", jpeg_bytes())
        location(full_reset(admin(apps)))
        # after the reset, the next event's import brings a student whose photo happens to have the same name
        key = store.save("new.jpg", jpeg_bytes())
        again = store.save("old.jpg", jpeg_bytes())
        make_student(engine, photo_path=again)
        make_student(engine, photo_path=key)
        fake.delete_fails = None
        location(admin(apps).post("/admin/system/reset/photo-cleanup", follow_redirects=False))
        assert fake.own() == {store.public_id(key), store.public_id(again)}

    def test_a_reset_interrupted_before_its_photo_cleanup_shows_as_unfinished(self, apps, engine):
        """What a server restart between COMMIT and the clean-up leaves behind: a DATA_RESET with no clean-up row."""
        fake = FakeCloudinaryAdmin()
        store = use_store(apps, fake.store())
        store.save("left-over.jpg", jpeg_bytes())
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO audit_log (action, operator_id, details) VALUES ('DATA_RESET', :u, "
                              "jsonb_build_object('reset_id', CAST(:r AS text), 'deleted', '{}'::jsonb))"),
                         {"u": admin_id(engine), "r": str(uuid.uuid4())})
        page = admin(apps).get("/admin/system").text
        assert 'data-last-reset="unfinished"' in page and "never finished" in page
        _, query = location(admin(apps).post("/admin/system/reset/photo-cleanup", follow_redirects=False))
        assert query["msg"].startswith("Photo clean-up finished") and fake.own() == set()

    def test_local_photo_storage_is_cleaned_inside_its_own_folder_only(self, apps, engine, sandbox, tmp_path):
        sandbox.root.mkdir(parents=True, exist_ok=True)
        for name in ("a.jpg", "b.PNG"):
            (sandbox.root / name).write_bytes(jpeg_bytes())
        (sandbox.root / "notes.txt").write_text("not a photo")
        (sandbox.root / ".half.part").write_bytes(b"x")
        (sandbox.root / "keep-me").mkdir()
        (sandbox.root / "keep-me" / "c.jpg").write_bytes(jpeg_bytes())
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(jpeg_bytes())
        _, query = location(full_reset(admin(apps)))
        assert query["msg"].endswith("2 photo(s) were removed from photo storage.")
        assert sorted(p.name for p in sandbox.root.iterdir()) == [".half.part", "keep-me", "notes.txt"]
        assert (sandbox.root / "keep-me" / "c.jpg").exists() and outside.exists()

    def test_the_cloudinary_purge_counts_an_unconfirmed_delete_as_failed(self):
        fake = FakeCloudinaryAdmin()
        store = fake.store()
        store.save("a.jpg", jpeg_bytes())
        store.save("b.jpg", jpeg_bytes())
        store._delete = lambda ids, **o: {"deleted": {ids[0]: "deleted"}, "partial": True}
        result = store.purge(set())
        assert (result.found, result.deleted, result.failed, result.complete) == (2, 1, 1, False)

    def test_owns_accepts_only_this_apps_naming_scheme(self):
        store = FakeCloudinaryAdmin().store()
        assert store.owns(store.public_id("photos/x.jpg"))
        for other in [*FOREIGN, None, "", FOLDER, FOLDER + "/", FOLDER + "/" + "A" * 32]:
            assert not store.owns(other)


# ══════════════════════════════════════════════════════════════════ one thing at a time
class TestLocking:
    def test_a_reset_refuses_to_start_while_an_import_is_committing(self, apps, engine):
        populate(engine)
        token = confirmation_of(ask(admin(apps)))
        before = data_counts(engine)
        with reset_svc.import_lock(engine):
            _, query = location(execute(admin(apps), token))
        assert "import or another reset is running" in query["error"]
        assert data_counts(engine) == before
        _, query = location(execute(admin(apps), token))                  # the same confirmation still works after
        assert "msg" in query

    def test_an_import_refuses_to_commit_while_a_reset_runs(self, engine):
        with reset_svc._reset_lock(engine):
            with pytest.raises(reset_svc.Busy, match="data reset is running"):
                with reset_svc.import_lock(engine):
                    pass
        with reset_svc.import_lock(engine), reset_svc.import_lock(engine):
            pass                                                         # two imports do not block each other

    def test_the_import_screen_says_so_in_one_sentence(self, apps, engine, tmp_path):
        from tests.test_import_screen import batch_of, choose_columns, csv_bytes, mapping_form, student_rows, upload
        client = admin(apps)
        batch = batch_of(upload(client, csv_bytes(student_rows([f"L{uuid.uuid4().hex[:8]}"]))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        with reset_svc._reset_lock(engine):
            path, query = location(client.post(f"/admin/import/{batch}/commit", follow_redirects=False))
        assert path == f"/admin/import/{batch}/preview"
        assert query["error"] == "A data reset is running. Please try the import again in a few minutes."


# ══════════════════════════════════════════════════════════════════ the record of it
class TestAudit:
    def test_the_reset_is_recorded_before_the_deletion_and_kept(self, apps, engine):
        populate(engine)
        n_students = data_counts(engine)["students"]
        location(full_reset(admin(apps)))
        record = rows(engine, "SELECT id, operator_id, reason, details FROM audit_log WHERE action = 'DATA_RESET' "
                              "ORDER BY id DESC LIMIT 1")[0]
        assert record["operator_id"] == admin_id(engine)
        assert record["details"]["deleted"]["students"] == n_students
        cleanup = rows(engine, "SELECT id, details FROM audit_log WHERE action = 'DATA_RESET_PHOTO_CLEANUP' ORDER BY id DESC LIMIT 1")[0]
        assert cleanup["details"]["reset_id"] == record["details"]["reset_id"] and cleanup["id"] > record["id"]
        requested = scalar(engine, "SELECT max(id) FROM audit_log WHERE action = 'DATA_RESET_REQUESTED'")
        assert requested < record["id"]                                     # the request came first and survived

    def test_a_second_reset_keeps_the_record_of_the_first(self, apps, engine):
        location(full_reset(admin(apps)))
        first = rows(engine, "SELECT id FROM audit_log WHERE action LIKE 'DATA_RESET%' ORDER BY id")
        populate(engine)
        location(full_reset(admin(apps)))
        after = {r["id"] for r in rows(engine, "SELECT id FROM audit_log WHERE action LIKE 'DATA_RESET%'")}
        assert {r["id"] for r in first} <= after
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'DATA_RESET'") >= 2

    def test_reset_records_cannot_be_deleted_afterwards(self, apps, engine):
        location(full_reset(admin(apps)))
        with pytest.raises(Exception, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM audit_log WHERE action LIKE 'DATA_RESET%'"))

    def test_the_audit_trail_page_shows_the_reset(self, apps, engine):
        location(full_reset(admin(apps)))
        page = admin(apps).get("/admin/audit?action=DATA_RESET").text
        assert "DATA_RESET" in page and "eng-admin" in page

    def test_reset_succeeds_and_clears_caller_dismissals(self, apps, engine):
        """Regression test: caller_dismissals has an FK to students and must be emptied by reset."""
        s = make_student(engine)
        u_id = admin_id(engine)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO caller_dismissals (student_id, dismissed_by) VALUES (:s, :u)"),
                {"s": s.id, "u": u_id}
            )
        assert scalar(engine, "SELECT count(*) FROM caller_dismissals") == 1

        location(full_reset(admin(apps)))

        assert scalar(engine, "SELECT count(*) FROM students") == 0
        assert scalar(engine, "SELECT count(*) FROM caller_dismissals") == 0


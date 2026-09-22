"""Shared test infrastructure.

The schema under test is ALWAYS built by the single documented command, `alembic upgrade head`,
run as a subprocess against the TEST database only. There is deliberately no second way to create
tables here: a fixture that builds its own approximation of the schema can silently create nothing
at all, and then every test that "passes" is passing against an empty database.
"""
import contextlib
import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Strict PostgreSQL default for test database
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://convocation_user:convocation_password@localhost:5432/convocation_test"
)


def ensure_postgres_test_db(url_str: str) -> None:
    """Ensure the target PostgreSQL test database exists."""
    url = make_url(url_str)
    if not url.drivername.startswith("postgresql"):
        return

    test_db_name = url.database
    # Connect to the maintenance/default database to check and create test DB
    admin_url = url.set(database="postgres" if url.database != "postgres" else "template1")
    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_db_name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{test_db_name}"'))
        admin_engine.dispose()
    except Exception:
        # If admin_url fails (e.g. specific user only has access to default db), try with original URL's default db
        pass


# --------------------------------------------------------------------------- #
# Building the schema: the documented `alembic upgrade head`, nothing else
# --------------------------------------------------------------------------- #
def _assert_is_test_database(url: str) -> None:
    """Refuse to DROP anything unless the target is clearly a test database."""
    db_name = make_url(url).database or ""
    assert db_name.endswith("_test") or "test" in db_name, (
        f"refusing to reset schema of non-test database {db_name!r}"
    )


def run_alembic(*args: str, database_url: str = TEST_DB_URL) -> subprocess.CompletedProcess:
    """Run the documented CLI (`alembic <args>`) against a TEST database."""
    _assert_is_test_database(database_url)
    env = os.environ.copy()
    env.update({"DATABASE_URL": database_url, "MODE": "venue", "VENUE_ID": "college"})
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def drop_everything(engine) -> None:
    _assert_is_test_database(str(engine.url))
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))


def rebuild_schema(engine) -> None:
    """A pristine schema, built the one documented way. Asserts that it really was built."""
    url = str(engine.url.render_as_string(hide_password=False))
    drop_everything(engine)
    result = run_alembic("upgrade", "head", database_url=url)
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture(scope="session", autouse=True)
def verify_test_database_separation():
    """Ensure the test database is strictly separated from dev/venue database."""
    lower_url = TEST_DB_URL.lower()
    if "convocation_db" in lower_url and "test" not in lower_url:
        raise RuntimeError(
            f"DANGER: TEST_DATABASE_URL '{TEST_DB_URL}' points to the production/dev database! "
            "Tests must be executed against a dedicated test database (e.g. convocation_test)."
        )
    ensure_postgres_test_db(TEST_DB_URL)


@pytest.fixture(scope="session")
def test_engine():
    """A test database migrated to head by the real `alembic upgrade head`.

    This used to call `Base.metadata.create_all`, which creates ZERO tables (the schema is defined
    in the migrations, not in SQLAlchemy models), so anything using this fixture was really running
    against an empty database. It is built the documented way now, and asserted to be non-empty.
    """
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
    rebuild_schema(engine)
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    assert {"students", "activity_events", "scan_log", "users"} <= tables, (
        f"the migrations did not build the schema; found only {sorted(tables)}"
    )
    yield engine
    drop_everything(engine)
    engine.dispose()


# --------------------------------------------------------------------------- #
# A database with NO schema: what a server looks like before the migrations run
# --------------------------------------------------------------------------- #
BARE_DB_NAME = "convocation_test_bare"


def _admin_engine():
    return create_engine(make_url(TEST_DB_URL).set(database="postgres"), isolation_level="AUTOCOMMIT")


def url_for_database(name: str) -> str:
    return make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)


def drop_database(name: str) -> None:
    _assert_is_test_database(url_for_database(name))
    from backend import database as _db                     # the app-wide engine cache is keyed by URL

    url = url_for_database(name)
    engine = _db._engines.pop(url, None)
    _db._sessionmakers.pop(url, None)
    if engine is not None:
        engine.dispose()
    admin = _admin_engine()
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@contextlib.contextmanager
def empty_database(name: str = BARE_DB_NAME):
    """A real, reachable, COMPLETELY EMPTY database: a server before `alembic upgrade head`."""
    drop_database(name)
    admin = _admin_engine()
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    url = url_for_database(name)
    engine = create_engine(url)
    try:
        with engine.connect() as c:                         # prove it really is empty
            assert [r[0] for r in c.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))] == []
        engine.dispose()
        yield url
    finally:
        engine.dispose()
        drop_database(name)


@pytest.fixture
def bare_database():
    with empty_database() as url:
        yield url

"""/health: what an operator's monitoring, and the first minute of a venue setup, rely on.

`db` answers three different questions with three different words, because they need three
different actions on the day:

    up          the database is reachable AND the schema is there: the server can take scans.
    not_ready   the database is reachable but `alembic upgrade head` has not been run: nothing
                works yet, and the fix is to run the migrations.
    down        the database cannot be reached at all.

A server whose migrations have never run used to answer "up", because the check was a bare
`SELECT 1`, which a completely empty database answers perfectly happily.
"""
import pytest
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend import database
from backend.config import Settings
from backend.main import create_app
from tests.conftest import TEST_DB_URL, run_alembic


def migrate(url: str) -> None:
    done = run_alembic("upgrade", "head", database_url=url)
    assert done.returncode == 0, done.stdout + done.stderr


def health(database_url: str, *, mode="venue", venue="college") -> dict:
    app = create_app(settings=Settings(mode=mode, venue_id=venue, database_url=database_url))
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    datetime.fromisoformat(body["timestamp"])
    return body


# --------------------------------------------------------------------------- #
# The schema is part of "up"
# --------------------------------------------------------------------------- #
def test_health_is_not_up_when_the_migrations_have_never_run(bare_database):
    """The connection is alive and `SELECT 1` works; there is simply no schema. That is NOT "up"."""
    assert database.check_db_health(database_url=bare_database) is True    # the connection itself is fine
    body = health(bare_database)
    assert body["db"] != "up"
    assert body["db"] == "not_ready"
    assert "students" in body["missing_tables"] and "scan_log" in body["missing_tables"]


def test_health_is_up_once_the_migrations_have_run(bare_database):
    migrate(bare_database)
    body = health(bare_database)
    assert body["db"] == "up"
    assert body.get("missing_tables") in (None, [])


def test_health_is_not_up_when_the_schema_is_only_half_there(bare_database, request):
    """A database with some tables but not the ones this server needs is still not ready."""
    engine = create_engine(bare_database)
    request.addfinalizer(engine.dispose)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE students (id int primary key)"))
    body = health(bare_database)
    assert body["db"] == "not_ready"
    assert "students" not in body["missing_tables"] and "scan_log" in body["missing_tables"]


# --------------------------------------------------------------------------- #
# Mode / venue reporting, against a REAL migrated database
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("venue", ["college", "stadium", "hall"])
def test_health_venue_modes(test_engine, venue):
    body = health(TEST_DB_URL, venue=venue)
    assert body["mode"] == "venue"
    assert body["venue"] == venue
    assert body["db"] == "up"


def test_health_central_mode(test_engine):
    body = health(TEST_DB_URL, mode="central", venue=None)
    assert body["mode"] == "central"
    assert body["venue"] is None
    assert body["db"] == "up"


def test_health_db_unreachable_reports_down_not_500():
    # Provide an unreachable database address without mock to test real recovery
    body = health("postgresql://invalid_user:invalid_pass@127.0.0.1:54329/nonexistent")
    assert body["mode"] == "venue"
    assert body["venue"] == "college"
    assert body["db"] == "down"


def test_health_real_db_live_check(test_engine):
    # Test against real reachable test engine
    body = health(TEST_DB_URL)
    assert body["mode"] == "venue"
    assert body["venue"] == "college"
    assert body["db"] == "up"


# --------------------------------------------------------------------------- #
# Settings validation
# --------------------------------------------------------------------------- #
def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="Invalid MODE"):
        Settings(mode="invalid_mode", venue_id="college", database_url=TEST_DB_URL)


def test_invalid_venue_rejected_in_venue_mode():
    with pytest.raises(ValueError, match="Unknown VENUE_ID"):
        Settings(mode="venue", venue_id="unknown_location", database_url=TEST_DB_URL)


def test_venue_mode_without_venue_id_rejected():
    with pytest.raises(ValueError, match="VENUE_ID is required"):
        Settings(mode="venue", venue_id=None, database_url=TEST_DB_URL)

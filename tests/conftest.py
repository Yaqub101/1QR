import os
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from backend.database import Base


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
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()

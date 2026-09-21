import os
import pytest
from sqlalchemy import create_engine
from backend.database import Base, get_engine


TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "sqlite:///./test_convocation_isolated.db")


@pytest.fixture(scope="session", autouse=True)
def verify_test_database_separation():
    """Ensure the test database is strictly separated from dev/venue database."""
    # Safety check: Prevent running tests against production or dev database
    lower_url = TEST_DB_URL.lower()
    if "convocation_db" in lower_url and "test" not in lower_url:
        raise RuntimeError(
            f"DANGER: TEST_DATABASE_URL '{TEST_DB_URL}' appears to point to the production/dev database! "
            "Tests must be executed against a dedicated test database."
        )


@pytest.fixture(scope="session")
def test_engine():
    connect_args = {"check_same_thread": False} if TEST_DB_URL.startswith("sqlite") else {}
    engine = create_engine(TEST_DB_URL, connect_args=connect_args)
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if TEST_DB_URL.startswith("sqlite:///./") and os.path.exists("./test_convocation_isolated.db"):
        try:
            os.remove("./test_convocation_isolated.db")
        except OSError:
            pass

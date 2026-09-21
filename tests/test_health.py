import os
import pytest
from datetime import datetime
from fastapi.testclient import TestClient
from unittest.mock import patch

from backend.main import create_app
from backend.config import Settings
from tests.conftest import TEST_DB_URL


def test_health_venue_college():
    settings = Settings(
        mode="venue",
        venue_id="college",
        database_url=TEST_DB_URL,
    )
    app = create_app(settings=settings)
    with patch("backend.database.check_db_health", return_value=True):
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "venue"
        assert data["venue"] == "college"
        assert data["db"] == "up"
        assert "timestamp" in data
        datetime.fromisoformat(data["timestamp"])


def test_health_venue_stadium():
    settings = Settings(
        mode="venue",
        venue_id="stadium",
        database_url=TEST_DB_URL,
    )
    app = create_app(settings=settings)
    with patch("backend.database.check_db_health", return_value=True):
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "venue"
        assert data["venue"] == "stadium"
        assert data["db"] == "up"
        assert "timestamp" in data
        datetime.fromisoformat(data["timestamp"])


def test_health_venue_hall():
    settings = Settings(
        mode="venue",
        venue_id="hall",
        database_url=TEST_DB_URL,
    )
    app = create_app(settings=settings)
    with patch("backend.database.check_db_health", return_value=True):
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "venue"
        assert data["venue"] == "hall"
        assert data["db"] == "up"
        assert "timestamp" in data
        datetime.fromisoformat(data["timestamp"])


def test_health_central_mode():
    settings = Settings(
        mode="central",
        venue_id=None,
        database_url=TEST_DB_URL,
    )
    app = create_app(settings=settings)
    with patch("backend.database.check_db_health", return_value=True):
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "central"
        assert data["venue"] is None
        assert data["db"] == "up"
        assert "timestamp" in data
        datetime.fromisoformat(data["timestamp"])


def test_health_db_unreachable_reports_down_not_500():
    # Provide an unreachable database address without mock to test real recovery
    settings = Settings(
        mode="venue",
        venue_id="college",
        database_url="postgresql://invalid_user:invalid_pass@127.0.0.1:54329/nonexistent",
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "venue"
    assert data["venue"] == "college"
    assert data["db"] == "down"
    assert "timestamp" in data
    datetime.fromisoformat(data["timestamp"])


def test_health_real_db_live_check(test_engine):
    # Test against real reachable test engine
    settings = Settings(
        mode="venue",
        venue_id="college",
        database_url=TEST_DB_URL,
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "venue"
    assert data["venue"] == "college"
    assert data["db"] == "up"
    assert "timestamp" in data
    datetime.fromisoformat(data["timestamp"])


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="Invalid MODE"):
        Settings(
            mode="invalid_mode",
            venue_id="college",
            database_url=TEST_DB_URL,
        )


def test_invalid_venue_rejected_in_venue_mode():
    with pytest.raises(ValueError, match="Unknown VENUE_ID"):
        Settings(
            mode="venue",
            venue_id="unknown_location",
            database_url=TEST_DB_URL,
        )


def test_venue_mode_without_venue_id_rejected():
    with pytest.raises(ValueError, match="VENUE_ID is required"):
        Settings(
            mode="venue",
            venue_id=None,
            database_url=TEST_DB_URL,
        )

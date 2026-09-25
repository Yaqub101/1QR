"""Tests for seeding faculty credentials from Excel / embedded fallback."""
import pathlib
import pytest
from sqlalchemy import text

from backend.config import Settings
from backend.security import passwords
from backend.security.login import attempt_login
from scripts.seed_faculty_credentials import (
    CREDENTIALS_DATA,
    parse_excel_credentials,
    seed_faculty_credentials,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCEL_PATH = REPO_ROOT / "Registration_Id_with_credentials.xlsx"


class TestFacultyCredentialsSeed:
    def test_excel_parsing_matches_embedded_data(self):
        """If the excel file exists, parsing it should match the embedded 20 entries."""
        if not EXCEL_PATH.exists():
            pytest.skip("Excel file not present on disk")
        parsed = parse_excel_credentials(str(EXCEL_PATH))
        assert len(parsed) == 20
        assert len(CREDENTIALS_DATA) == 20
        for p, e in zip(parsed, CREDENTIALS_DATA):
            assert p["username"] == e["username"]
            assert p["password"] == e["password"]
            assert p["dept"] == e["dept"]

    def test_seed_creates_and_updates_users(self, test_engine):
        """Test seed creating 20 users, and updating an existing user with new password."""
        settings = Settings()

        # Clean up any test registry users first
        with test_engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_log WHERE action IN ('USER_CREATED', 'PASSWORD_RESET', 'LOGIN')"))
            conn.execute(text("DELETE FROM users WHERE role = 'REGISTRY'"))

        # 1. First run: should create all 20 accounts
        res1 = seed_faculty_credentials(test_engine, CREDENTIALS_DATA)
        assert len(res1["created"]) == 20
        assert len(res1["updated"]) == 0

        # Verify in DB and test login for all 20 accounts
        with test_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT username, password_hash, role, full_name, active FROM users WHERE role = 'REGISTRY'")
            ).mappings().all()
            assert len(rows) == 20

            # Verify that every account can log in with its assigned password
            for item in CREDENTIALS_DATA:
                login_res = attempt_login(
                    conn,
                    settings=settings,
                    username=item["username"],
                    password=item["password"],
                )
                assert login_res.error is None
                assert login_res.role == "REGISTRY"
                assert login_res.username == item["username"]

        # 2. Modify one password in dataset to test UPDATE
        modified_data = [dict(c) for c in CREDENTIALS_DATA]
        modified_data[0]["password"] = "NewPass123!"

        res2 = seed_faculty_credentials(test_engine, modified_data)
        assert len(res2["created"]) == 0
        assert len(res2["updated"]) == 20

        with test_engine.connect() as conn:
            # Arch password changed: old password fails, new password succeeds
            login_old = attempt_login(
                conn,
                settings=settings,
                username="arch_rs",
                password="XST1678%",
            )
            assert login_old.error is not None

            login_new = attempt_login(
                conn,
                settings=settings,
                username="arch_rs",
                password="NewPass123!",
            )
            assert login_new.error is None
            assert login_new.username == "arch_rs"

        # 3. Clean up / Restore original
        seed_faculty_credentials(test_engine, CREDENTIALS_DATA)
        with test_engine.connect() as conn:
            login_restored = attempt_login(
                conn,
                settings=settings,
                username="arch_rs",
                password="XST1678%",
            )
            assert login_restored.error is None

    def test_seed_transaction_rolls_back_on_invalid_password(self, test_engine):
        """If any password in the batch is invalid, nothing should be modified."""
        bad_data = [
            {"sr": 99, "dept": "Bad", "username": "bad_rs", "password": "123", "coordinators": []}  # < 8 chars
        ]
        with pytest.raises(passwords.WeakPasswordError):
            seed_faculty_credentials(test_engine, bad_data)

        with test_engine.connect() as conn:
            bad_user = conn.execute(
                text("SELECT 1 FROM users WHERE username = 'bad_rs'")
            ).scalar()
            assert bad_user is None

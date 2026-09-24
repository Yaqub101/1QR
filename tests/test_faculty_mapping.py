"""
Tests for faculty mapping, normalization, database table, and admin assign-faculty.
"""
from typing import Any
import pytest
from sqlalchemy import text
from backend import users as users_svc
from backend.faculty_map import (
    Faculty,
    DEFAULT_PROGRAMME_MAP,
    ERP_SCHOOL_TO_FACULTY,
    derive_faculty,
    normalize_name,
    get_faculty_palette,
)
from tests.test_auth import PASSWORD, api_login, new_client
from tests.test_station_engine import apps, engine, world


def test_normalize_name():
    assert normalize_name("  B.Tech.   (Computer  Science)  ") == "b.tech. (computer science)"
    assert normalize_name("M.Sc  Physics (see Note 1)") == "m.sc physics"
    assert normalize_name("M.Sc  Physics (see note 2)") == "m.sc physics"
    assert normalize_name("BA (Hons)  Music  ") == "ba (hons) music"


def test_all_108_default_programmes_resolve_to_non_unmapped():
    """All 108 programmes must map to one of the 7 official faculties (never UNMAPPED)."""
    assert len(DEFAULT_PROGRAMME_MAP) == 108
    valid_faculties = {
        Faculty.SCIENCE.value,
        Faculty.ENGINEERING.value,
        Faculty.MANAGEMENT.value,
        Faculty.SOCIAL_SCI.value,
        Faculty.DESIGN.value,
        Faculty.INTERDISCIPLINARY.value,
        Faculty.PERFORMING_ARTS.value,
    }

    for prog_name, fac in DEFAULT_PROGRAMME_MAP.items():
        assert fac in valid_faculties, f"Programme '{prog_name}' mapped to invalid/unmapped faculty '{fac}'"


def test_erp_school_mappings():
    assert ERP_SCHOOL_TO_FACULTY[normalize_name("School of Engineering and Technology")] == Faculty.ENGINEERING.value
    assert ERP_SCHOOL_TO_FACULTY[normalize_name("School of Basic and Applied Sciences")] == Faculty.SCIENCE.value
    assert ERP_SCHOOL_TO_FACULTY[normalize_name("Institute of Management and Research")] == Faculty.MANAGEMENT.value
    assert ERP_SCHOOL_TO_FACULTY[normalize_name("Institute of Design")] == Faculty.DESIGN.value
    assert ERP_SCHOOL_TO_FACULTY[normalize_name("Mahagami Gurukul")] == Faculty.PERFORMING_ARTS.value


def test_derive_faculty_precedence():
    # 1. Override wins over ERP school!
    prog = "B.Sc. Chemistry"
    norm_prog = normalize_name(prog)
    fac = derive_faculty(
        "School of Engineering and Technology",
        prog,
        custom_map={norm_prog: Faculty.MANAGEMENT.value},
        overrides={norm_prog},
    )
    assert fac == Faculty.MANAGEMENT.value

    # 2. Without override, valid ERP school wins over fallback programme mapping
    fac = derive_faculty("School of Engineering and Technology", "B.Sc. Chemistry")
    assert fac == Faculty.ENGINEERING.value

    # 3. Unknown or missing ERP school falls back to programme mapping
    fac = derive_faculty("", "B.Tech. (Computer Science and Engineering)")
    assert fac == Faculty.ENGINEERING.value

    fac = derive_faculty(None, "Bachelor of Design (Fashion Design)")
    assert fac == Faculty.DESIGN.value

    # 4. Both unknown yields UNMAPPED
    fac = derive_faculty("Nonexistent School", "Unknown Weird Degree 123")
    assert fac == Faculty.UNMAPPED.value


def test_faculty_palette_lookup():
    p_sci = get_faculty_palette(Faculty.SCIENCE.value)
    assert p_sci["strong"] == "#278844"
    assert p_sci["light"] == "#9DD29C"

    p_eng = get_faculty_palette(Faculty.ENGINEERING.value)
    assert p_eng["strong"] == "#134B90"
    assert p_eng["light"] == "#9EC8E9"

    p_unm = get_faculty_palette("UNKNOWN")
    assert p_unm["strong"] == "#58595B"
    assert p_unm["light"] == "#E6E7E8"


def test_db_all_programmes_resolve(engine):
    """Verify that in the database, all programme_faculty rows resolve to non-UNMAPPED."""
    with engine.begin() as c:
        rows = c.execute(
            text("SELECT programme_name, faculty FROM programme_faculty")
        ).fetchall()
        assert len(rows) >= 108
        for prog, fac in rows:
            assert fac != Faculty.UNMAPPED.value, f"{prog} in DB is UNMAPPED"


def test_admin_assign_and_clear_faculty_api(apps, engine, world):
    """Admin can reassign faculty to a programme with override and clear it."""
    with engine.begin() as c:
        try:
            users_svc.create_user(c, username="fac-admin", password=PASSWORD, role="ADMIN")
        except Exception:
            pass

    client = new_client(apps)
    assert api_login(client, "fac-admin").status_code == 200

    unique_prog = "B.Test Specialist In Quantum Logic"
    norm_prog = normalize_name(unique_prog)

    with engine.begin() as c:
        c.execute(text("DELETE FROM programme_faculty WHERE programme_key = :p"), {"p": norm_prog})
        c.execute(text("DELETE FROM students WHERE prn = 'PRN-FAC-TEST'"))
        c.execute(
            text(
                """
                INSERT INTO students (id, prn, name, programme, school, faculty, status)
                VALUES (gen_random_uuid(), 'PRN-FAC-TEST', 'Fac Tester', :prog, 'Engineering & Technology', 'ENGINEERING', 'ACTIVE')
                """
            ),
            {"prog": unique_prog}
        )

    # 1. Admin assigns override (even though school is Engineering & Technology)
    resp = client.post(
        "/admin/api/assign-faculty",
        json={
            "programme_name": unique_prog,
            "faculty": Faculty.INTERDISCIPLINARY.value,
            "is_override": True,
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["faculty"] == Faculty.INTERDISCIPLINARY.value
    assert data["is_override"] is True

    # Student faculty re-derived to INTERDISCIPLINARY because of override
    with engine.begin() as c:
        stud = c.execute(
            text("SELECT faculty FROM students WHERE prn = 'PRN-FAC-TEST'")
        ).fetchone()
        assert stud[0] == Faculty.INTERDISCIPLINARY.value

    # 2. Clear override -> student reverts to school's faculty (ENGINEERING)
    resp_clear = client.post(
        "/admin/api/clear-override",
        json={"programme_name": unique_prog}
    )
    assert resp_clear.status_code == 200, resp_clear.text
    assert resp_clear.json()["ok"] is True

    with engine.begin() as c:
        stud = c.execute(
            text("SELECT faculty FROM students WHERE prn = 'PRN-FAC-TEST'")
        ).fetchone()
        assert stud[0] == Faculty.ENGINEERING.value

        # Clean up
        c.execute(text("DELETE FROM students WHERE prn = 'PRN-FAC-TEST'"))
        c.execute(text("DELETE FROM programme_faculty WHERE programme_key = :p"), {"p": norm_prog})

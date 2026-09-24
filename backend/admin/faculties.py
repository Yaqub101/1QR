"""backend/admin/faculties.py — Admin faculty management service and endpoints.

Allows administrators to:
- List all programmes and their mapped faculties
- View UNMAPPED programmes and count of affected students
- Assign or update a faculty for any programme
- Re-derive students' faculty without redeploy
"""
from __future__ import annotations

import json
from typing import Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.audit import write_audit
from backend.faculty_map import ERP_SCHOOL_TO_FACULTY, Faculty, PALETTE, normalize_name
from backend.security.deps import http_error, require_admin
from backend.security.sessions import Principal
from backend.web import render

router = APIRouter(prefix="/admin")


class AssignFacultyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    programme: Optional[str] = None
    programme_name: Optional[str] = None
    faculty: str
    is_override: Optional[bool] = True
    override_school: Optional[bool] = None


class ClearOverrideBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    programme: Optional[str] = None
    programme_name: Optional[str] = None


ERP_SCHOOL_NAMES = [
    "Basic and Applied Sciences",
    "Engineering & Technology",
    "Faculty of Engineering and Technology",
    "Management and Commerce",
    "Social Sciences and Humanities",
    "Faculty of Design",
    "Faculty of Interdisciplinary Studies",
    "Faculty of Performing Arts",
]


def list_programme_faculties(conn: Connection) -> list[dict]:
    """Return all distinct programmes from students and programme_faculty, with counts and assigned faculty."""
    sql = """
        WITH student_progs AS (
            SELECT
                regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g') AS prog_key,
                min(programme) AS sample_name,
                count(*) AS student_count,
                count(*) FILTER (WHERE faculty = 'UNMAPPED') AS unmapped_count
            FROM students
            GROUP BY regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g')
        )
        SELECT
            coalesce(pf.programme_key, sp.prog_key) AS programme_key,
            coalesce(pf.programme_name, sp.sample_name, pf.programme_key) AS programme_name,
            coalesce(pf.faculty, 'UNMAPPED') AS faculty,
            coalesce(sp.student_count, 0) AS student_count,
            coalesce(sp.unmapped_count, 0) AS unmapped_count,
            coalesce(pf.is_override, false) AS is_override,
            pf.updated_at
        FROM programme_faculty pf
        FULL OUTER JOIN student_progs sp ON pf.programme_key = sp.prog_key
        ORDER BY unmapped_count DESC, student_count DESC, programme_name ASC
    """
    rows = conn.execute(text(sql)).mappings().all()
    return [
        {
            "programme_key": r["programme_key"],
            "programme_name": r["programme_name"],
            "faculty": r["faculty"],
            "student_count": r["student_count"],
            "unmapped_count": r["unmapped_count"],
            "is_override": bool(r.get("is_override", False)),
            "palette": PALETTE.get(r["faculty"], PALETTE[Faculty.UNMAPPED.value]),
        }
        for r in rows
    ]


def assign_faculty_to_programme(
    conn: Connection,
    *,
    programme: str,
    faculty: str,
    operator_id: Optional[str] = None,
    is_override: bool = True,
    override_school: Optional[bool] = None,
) -> dict:
    """Upsert programme mapping in programme_faculty and re-derive affected students."""
    prog_key = normalize_name(programme)
    if not prog_key:
        raise ValueError("Programme name cannot be blank.")

    faculty_upper = faculty.strip().upper()
    if faculty_upper not in PALETTE:
        raise ValueError(f"Invalid faculty '{faculty}'. Must be one of: {list(PALETTE.keys())}")

    effective_override = is_override if override_school is None else (override_school or is_override)

    # Upsert in programme_faculty table with is_override
    conn.execute(
        text(
            """
            INSERT INTO programme_faculty (programme_key, programme_name, faculty, is_override, updated_at)
            VALUES (:k, :n, :f, :o, now())
            ON CONFLICT (programme_key) DO UPDATE
            SET faculty = EXCLUDED.faculty,
                programme_name = EXCLUDED.programme_name,
                is_override = EXCLUDED.is_override,
                updated_at = now()
            """
        ),
        {"k": prog_key, "n": programme.strip(), "f": faculty_upper, "o": effective_override},
    )

    # Re-derive affected students
    if effective_override:
        res = conn.execute(
            text(
                """
                UPDATE students
                SET faculty = :f
                WHERE regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g') = :k
                """
            ),
            {"f": faculty_upper, "k": prog_key},
        )
    else:
        res = conn.execute(
            text(
                """
                UPDATE students
                SET faculty = :f
                WHERE regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g') = :k
                  AND (school IS NULL OR school NOT IN :erp_schools)
                """
            ),
            {"f": faculty_upper, "k": prog_key, "erp_schools": tuple(ERP_SCHOOL_NAMES)},
        )

    affected = res.rowcount
    if operator_id:
        write_audit(
            conn,
            "ASSIGN_FACULTY",
            operator_id=operator_id,
            details={
                "programme": programme,
                "programme_key": prog_key,
                "faculty": faculty_upper,
                "affected_students": affected,
                "is_override": effective_override,
            },
        )

    # Broadcast event if PostgreSQL
    if conn.dialect.name == "postgresql":
        try:
            conn.execute(
                text(
                    "SELECT pg_notify('queue_events', json_build_object('action', 'FACULTY_CHANGE', 'programme_key', :k, 'faculty', :f)::text)"
                ),
                {"k": prog_key, "f": faculty_upper},
            )
        except Exception:
            pass

    return {
        "ok": True,
        "programme_key": prog_key,
        "faculty": faculty_upper,
        "affected_students": affected,
        "is_override": effective_override,
    }


def clear_override_for_programme(
    conn: Connection,
    *,
    programme: str,
    operator_id: Optional[str] = None,
) -> dict:
    """Clear admin override for a programme and restore default derivation."""
    prog_key = normalize_name(programme)
    if not prog_key:
        raise ValueError("Programme name cannot be blank.")

    conn.execute(
        text("UPDATE programme_faculty SET is_override = false, updated_at = now() WHERE programme_key = :k"),
        {"k": prog_key},
    )

    # Restore ERP school where school is an ERP school
    for school_name in ERP_SCHOOL_NAMES:
        school_fac = ERP_SCHOOL_TO_FACULTY.get(normalize_name(school_name))
        if school_fac:
            conn.execute(
                text(
                    """
                    UPDATE students
                    SET faculty = :f
                    WHERE regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g') = :k
                      AND school = :s
                    """
                ),
                {"f": school_fac, "k": prog_key, "s": school_name},
            )

    # Students with unknown or missing ERP school fall back to baseline programme mapping
    from backend.faculty_map import DEFAULT_PROGRAMME_MAP
    default_fac = DEFAULT_PROGRAMME_MAP.get(prog_key, Faculty.UNMAPPED.value)
    conn.execute(
        text(
            """
            UPDATE students
            SET faculty = :f
            WHERE regexp_replace(lower(trim(programme)), '\\s+', ' ', 'g') = :k
              AND (school IS NULL OR school NOT IN :erp_schools)
            """
        ),
        {"f": default_fac, "k": prog_key, "erp_schools": tuple(ERP_SCHOOL_NAMES)},
    )

    if operator_id:
        write_audit(
            conn,
            "CLEAR_FACULTY_OVERRIDE",
            operator_id=operator_id,
            details={"programme": programme, "programme_key": prog_key},
        )

    if conn.dialect.name == "postgresql":
        try:
            conn.execute(
                text(
                    "SELECT pg_notify('queue_events', json_build_object('action', 'FACULTY_CHANGE', 'programme_key', :k)::text)"
                ),
                {"k": prog_key},
            )
        except Exception:
            pass

    return {"ok": True, "programme_key": prog_key}


@router.get("/faculties")
def faculties_page(request: Request, principal: Principal = Depends(require_admin)):
    """Render the admin faculty management page."""
    with request.app.state.engine.connect() as conn:
        items = list_programme_faculties(conn)
        total_progs = len(items)
        unmapped_progs = sum(1 for it in items if it["faculty"] == "UNMAPPED")
        total_students = sum(it["student_count"] for it in items)
        unmapped_students = sum(it["unmapped_count"] for it in items)

    faculties_list = [f.value for f in Faculty]
    return render(
        request,
        "admin_faculties.html",
        principal=principal,
        items=items,
        faculties=faculties_list,
        palette=PALETTE,
        total_progs=total_progs,
        unmapped_progs=unmapped_progs,
        total_students=total_students,
        unmapped_students=unmapped_students,
        active_nav="faculties",
    )


@router.get("/api/faculties")
def api_list_faculties(request: Request, principal: Principal = Depends(require_admin)):
    """JSON API to list all programme-faculty mappings and summary statistics."""
    with request.app.state.engine.connect() as conn:
        items = list_programme_faculties(conn)
    return {
        "programmes": items,
        "faculties": [f.value for f in Faculty],
        "palette": PALETTE,
        "unmapped_count": sum(1 for it in items if it["faculty"] == "UNMAPPED"),
    }


@router.post("/api/assign-faculty")
def api_assign_faculty(
    body: AssignFacultyBody,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    """Assign a faculty to a programme and immediately re-derive affected students."""
    prog = (body.programme or body.programme_name or "").strip()
    if not prog:
        raise http_error(400, "MISSING_PROGRAMME", "Programme name cannot be blank.")
    try:
        with request.app.state.engine.begin() as conn:
            result = assign_faculty_to_programme(
                conn,
                programme=prog,
                faculty=body.faculty,
                operator_id=principal.user_id,
                is_override=bool(body.is_override),
                override_school=body.override_school,
            )
        return result
    except ValueError as exc:
        raise http_error(400, "INVALID_FACULTY", str(exc)) from exc
    except Exception as exc:
        raise http_error(500, "ASSIGN_FAILED", "Failed to assign faculty.") from exc


@router.post("/api/clear-override")
def api_clear_override(
    body: ClearOverrideBody,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    """Clear override on a programme and restore default derivation."""
    prog = (body.programme or body.programme_name or "").strip()
    if not prog:
        raise http_error(400, "MISSING_PROGRAMME", "Programme name cannot be blank.")
    try:
        with request.app.state.engine.begin() as conn:
            result = clear_override_for_programme(
                conn,
                programme=prog,
                operator_id=principal.user_id,
            )
        return result
    except ValueError as exc:
        raise http_error(400, "INVALID_PROGRAMME", str(exc)) from exc
    except Exception as exc:
        raise http_error(500, "CLEAR_FAILED", "Failed to clear faculty override.") from exc

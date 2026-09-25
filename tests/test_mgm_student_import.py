"""Tests for MGM University student import, header detection, column aliases,
duplicate handling, department-wise QR pass generation, and scanning continuity.
"""
from __future__ import annotations

import io
import re
from urllib.parse import quote

import pandas as pd
import pytest
from sqlalchemy import text

from backend.admin.routes import passes_page
from backend.database import get_engine
from backend.engine.pipeline import identify_by_token
from backend.importer import (
    _COLUMN_ALIASES,
    check_required_fields_satisfied,
    commit_import,
    detect_column_mapping,
    detect_header_row,
    normalize_key,
    parse_file,
    validate_import,
)
from backend.passes import list_departments, load_passes, sanitize_department_filename
from backend.qr_tokens import generate_missing_tokens
from tests.test_station_engine import admin, apps, engine, world


def _make_excel_bytes(rows_above: list[list[str]], header: list[str], data_rows: list[list[str]]) -> bytes:
    all_rows = []
    for r in rows_above:
        all_rows.append(r)
    all_rows.append(header)
    for r in data_rows:
        all_rows.append(r)
    df = pd.DataFrame(all_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, header=False, index=False)
    return buf.getvalue()


class TestHeaderDetection:
    def test_detect_header_row_with_title_lines(self):
        title_rows = [
            ["MGM University, Chhatrapati Sambhajinagar", "", "", "", ""],
            ["Convocation Student List 2026", "", "", "", ""],
            ["Generated on 25-09-2026", "", "", "", ""],
            ["", "", "", "", ""],
        ]
        header = ["Sr.No", "PRN No.", "Student First Name", "Student Last Name", "Department Name", "Programme Name"]
        data = [
            ["1", "PRN9001", "Aarav", "Sharma", "Computer Science", "B.Tech CSE"],
            ["2", "PRN9002", "Priya", "Patel", "Computer Science", "B.Tech CSE"],
        ]
        raw_bytes = _make_excel_bytes(title_rows, header, data)

        row_index = detect_header_row(raw_bytes, "report.xlsx")
        assert row_index == 4

        cols, rows = parse_file(raw_bytes, "report.xlsx")
        assert cols == ["Sr.No", "PRN No.", "Student First Name", "Student Last Name", "Department Name", "Programme Name"]
        assert len(rows) == 2
        assert rows[0]["PRN No."] == "PRN9001"
        assert rows[0]["Student First Name"] == "Aarav"

    def test_detect_header_row_zero_when_already_at_top(self):
        header = ["PRN No.", "Student First Name", "Student Last Name", "Department Name", "Programme Name"]
        data = [["PRN9001", "Aarav", "Sharma", "Computer Science", "B.Tech CSE"]]
        raw_bytes = _make_excel_bytes([], header, data)
        row_index = detect_header_row(raw_bytes, "report.xlsx")
        assert row_index == 0


class TestColumnAliasesAndNormalization:
    def test_normalize_key_variants(self):
        assert normalize_key("Programme Name") == "programme name"
        assert normalize_key("Program   Name") == "program name"
        assert normalize_key("Department  Name.") == "department name"
        assert normalize_key("PRN No.") == "prn no"
        assert normalize_key("PRN") == "prn"
        assert normalize_key("Enrollment No/Roll No") == "enrollment no roll no"
        assert normalize_key("Enrollment No / Roll No") == "enrollment no roll no"
        assert normalize_key("Student First Name") == "student first name"
        assert normalize_key("Sr.No") == "sr no"
        assert normalize_key("Serial No.") == "serial no"
        assert normalize_key("Mobile No.") == "mobile no"
        assert normalize_key("Email Id") == "email id"

    def test_detect_column_mapping_recognized_aliases(self):
        cols = [
            "Sr No.",
            "Programme Name",
            "Department Name",
            "PRN No.",
            "Enrollment No/Roll No",
            "Student First Name",
            "Student Middle Name",
            "Student Last Name",
            "Student Mother Name",
            "Student Father Name",
            "Gender",
            "Mobile No.",
            "Email Id",
            "Unknown University Column",
            "Aadhar Card No",
        ]
        mapping = detect_column_mapping(cols)
        assert mapping["Sr No."] == "sr_no"
        assert mapping["Programme Name"] == "programme"
        assert mapping["Department Name"] == "department"
        assert mapping["PRN No."] == "prn"
        assert mapping["Enrollment No/Roll No"] == "enrollment_no"
        assert mapping["Student First Name"] == "first_name"
        assert mapping["Student Middle Name"] == "middle_name"
        assert mapping["Student Last Name"] == "last_name"
        assert mapping["Student Mother Name"] == "mother_name"
        assert mapping["Student Father Name"] == "father_name"
        assert mapping["Gender"] == "gender"
        assert mapping["Mobile No."] == "mobile"
        assert mapping["Email Id"] == "email"
        assert "Unknown University Column" not in mapping
        assert "Aadhar Card No" not in mapping

        is_satisfied, missing = check_required_fields_satisfied(mapping.values())
        assert is_satisfied is True
        assert missing == []


class TestReportFormatsAndImport:
    def test_paid_report_format_import(self, engine):
        title_rows = [["MGM University", "", "", "", "", "", "", ""]]
        header = [
            "Sr.No", "PRN No.", "Student First Name", "Student Middle Name", "Student Last Name",
            "Program Name", "Stream", "Email Id", "Mobile No.", "Payment Status",
        ]
        data = [
            ["101", "2026MGM001", "Rahul", "V", "Deshmukh", "B.Tech Computer Science", "JNEC CSE", "rahul@test.org", "9876543210", "PAID"],
            ["102", "2026MGM002", "Sneha", "", "Kulkarni", "B.Tech Civil Engineering", "JNEC Civil", "sneha@test.org", "9876543211", "PAID"],
        ]
        raw_bytes = _make_excel_bytes(title_rows, header, data)
        cols, rows = parse_file(raw_bytes, "paid.xlsx")
        mapping = detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            assert preview.is_valid is True
            assert len(preview.to_create) == 2
            assert preview.to_create[0]["name"] == "Rahul V Deshmukh"
            assert preview.to_create[0]["school"] == "JNEC CSE"
            assert preview.to_create[0]["sr_no"] == "101"

            summary = commit_import(preview, conn, filename="paid.xlsx")
            assert summary.created == 2

        with engine.connect() as conn:
            db_row = conn.execute(text("SELECT name, school, sr_no FROM students WHERE prn = '2026MGM001'")).mappings().one()
            assert db_row["name"] == "Rahul V Deshmukh"
            assert db_row["school"] == "JNEC CSE"
            assert db_row["sr_no"] == "101"

    def test_unpaid_report_format_import(self, engine):
        title_rows = [["MGM University Unpaid Report", "", "", "", "", "", ""]]
        header = [
            "Sr No", "Enrollment No/Roll No", "Student First Name", "Student Last Name",
            "Programme Name", "Department Name", "Mobile No", "Category", "Address",
        ]
        data = [
            ["201", "2026MGM003", "Ananya", "Joshi", "MBA Finance", "Management Studies", "9876543212", "OPEN", "Aurangabad"],
        ]
        raw_bytes = _make_excel_bytes(title_rows, header, data)
        cols, rows = parse_file(raw_bytes, "unpaid.xlsx")
        mapping = detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            assert preview.is_valid is True
            assert len(preview.to_create) == 1
            assert preview.to_create[0]["prn"] == "2026MGM003"
            assert preview.to_create[0]["name"] == "Ananya Joshi"
            assert preview.to_create[0]["school"] == "Management Studies"
            assert preview.to_create[0]["sr_no"] == "201"

            summary = commit_import(preview, conn, filename="unpaid.xlsx")
            assert summary.created == 1


class TestDuplicateHandling:
    def test_in_file_duplicate_with_differing_sr_no_is_skipped_not_error(self, engine):
        header = ["Sr.No", "PRN No.", "Student First Name", "Student Last Name", "Department Name", "Programme Name"]
        data = [
            ["501", "2026MGM004", "Vikram", "Rathod", "Mechanical Engineering", "B.Tech Mech"],
            ["502", "2026MGM004", "Vikram", "Rathod", "Mechanical Engineering", "B.Tech Mech"],
        ]
        raw_bytes = _make_excel_bytes([], header, data)
        cols, rows = parse_file(raw_bytes, "dups.xlsx")
        mapping = detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            assert preview.is_valid is True
            assert len(preview.to_create) == 1
            assert len(preview.to_skip) == 1
            assert preview.to_create[0]["sr_no"] == "501"

            summary = commit_import(preview, conn, filename="dups.xlsx")
            assert summary.created == 1
            assert summary.skipped == 1

    def test_importing_existing_student_is_deduplicated_by_prn(self, engine):
        header = ["Sr.No", "PRN No.", "Student First Name", "Student Last Name", "Department Name", "Programme Name"]
        data = [
            ["999", "2026MGM004", "Vikram", "Rathod", "Mechanical Engineering", "B.Tech Mech"],
        ]
        raw_bytes = _make_excel_bytes([], header, data)
        cols, rows = parse_file(raw_bytes, "reupload.xlsx")
        mapping = detect_column_mapping(cols)

        with engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            assert preview.is_valid is True
            assert len(preview.to_create) == 0
            assert len(preview.to_skip) == 1
            assert any(f.type == "existing_student" for f in preview.flagged_duplicates)


class TestDepartmentQRGenerationAndScanning:
    def test_sanitize_department_filename(self):
        assert sanitize_department_filename("JNEC Computer Science Engineering") == "JNEC_Computer_Science_Engineering"
        assert sanitize_department_filename("Dept / CS <AI> : 2026*") == "Dept_CS_AI_2026"
        assert sanitize_department_filename("") == "Department"

    def test_department_grouping_and_download(self, apps, engine):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO students (prn, name, programme, school, status, sr_no)
                    VALUES ('2026DEPT01', 'Alice CS', 'B.Tech CSE', 'JNEC Computer Science', 'ACTIVE', '1'),
                           ('2026DEPT02', 'Bob CS', 'B.Tech CSE', 'JNEC Computer Science', 'ACTIVE', '2'),
                           ('2026DEPT03', 'Charlie ME', 'B.Tech ME', 'JNEC Mechanical', 'ACTIVE', '3')
                    ON CONFLICT (prn) DO NOTHING
                    """
                )
            )

        with engine.connect() as conn:
            depts = list_departments(conn)
            dept_names = {d["department"]: d["total_students"] for d in depts}
            assert dept_names.get("JNEC Computer Science") == 2
            assert dept_names.get("JNEC Mechanical") == 1

        # Generate tokens
        generate_missing_tokens(engine)

        client = admin(apps, "college")
        resp = client.get("/admin/passes/department?name=JNEC+Computer+Science")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert 'filename="QR_JNEC_Computer_Science.pdf"' in resp.headers["content-disposition"]
        assert len(resp.content) > 1000

    def test_imported_student_qr_scanning_flow(self, engine):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO students (prn, name, programme, school, status, sr_no)
                    VALUES ('2026SCAN01', 'Test Scanner Student', 'B.Tech CSE', 'JNEC CSE', 'ACTIVE', '99')
                    ON CONFLICT (prn) DO UPDATE SET status = 'ACTIVE'
                    """
                )
            )

        generate_missing_tokens(engine)

        with engine.connect() as conn:
            token = conn.execute(
                text(
                    """
                    SELECT t.token FROM qr_tokens t
                    JOIN students s ON s.id = t.student_id
                    WHERE s.prn = '2026SCAN01' AND t.active
                    """
                )
            ).scalar_one()

            student, outcome = identify_by_token(conn, token)
            assert outcome is None  # Successful identification has no error outcome
            assert student is not None
            assert student["prn"] == "2026SCAN01"
            assert student["name"] == "Test Scanner Student"

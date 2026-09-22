"""A saved import mapping preset for the university's real "Student Convocation Detail Report"
export (.xls, header on the physical 5th row, 39 named columns), and the two behaviour changes it
needed in the importer itself.

  1. THE HARD RULE. Aadhar number, address, guardian details, payment details, courier address,
     the Marathi name fields, alternate contact details, CGPA, last-exam grade/year, batch name,
     date of admission, form submission date and "certificate by courier" must never be mapped,
     stored, logged or displayed -- anywhere, including logs and audit entries. This is enforced as
     an ALLOWLIST (`backend/import_presets.py`): a matched file is projected down to ONLY the six
     safe fields (plus a constant status) at the parsing step, before a row becomes a Python dict
     any later code -- including the mapping screen's own "first values" sample -- could read. What
     is tested here is not "the screen hides it": it is "the value was never there to hide".

  2. A duplicate PRN that repeats an EARLIER ROW EXACTLY (the real file's own quirk: PRN
     202308116012 appears twice at rows 766/767, same student, same everything, only the running
     Sr.No differs) is a normal duplicate-skip, like a student already on the list, and must not
     block the rest of the file. A duplicate PRN whose rows DISAGREE on any mapped field is still a
     fatal error: nothing here can guess which one is right.

THE REAL FILE. `backend/import_presets.HEADER_COLUMNS` was reconstructed from a written description
before any file was attached, and was corrected against the real export once one arrived (it
mirrors ALL FIVE English name fields into Marathi, not four, and "Enrollment No/Roll No" is one
literal column, not two -- both differ from the first, purely-descriptive guess). `TestAgainstTheRealFile`
below drives the actual attached file end to end and is the strongest evidence available that the
preset is right; it is skipped (not failed) wherever that file is not present, because the file is
real students' personal data and must never be committed to this repository (see `.gitignore`) --
everything else in this module proves the same properties with a synthetic fixture instead.
"""
from __future__ import annotations

import io
import pathlib
import re
import uuid

import pandas as pd
import pytest
from sqlalchemy import text

from backend import import_presets
from backend.importer import commit_import, read_and_validate, validate_import
from tests.admin_support import rows, scalar
from tests.test_import_screen import (
    batch_of,
    choose_columns,
    commit as commit_batch,
    csv_bytes,
    mapping_form,
    numbers,
    preview_page,
    summary_page,
    upload,
)
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    world,
)

xlwt = pytest.importorskip("xlwt")
pytest.importorskip("xlrd")

SENTINEL_PREFIX = "SENTINEL-"

# One fixed, unmistakable marker per denylisted column, all sharing one prefix so "did anything
# marked SENTINEL- survive anywhere" is a single check. `None` marks the six fields this preset is
# actually allowed to keep (plus Sr.No, which is neither sensitive nor kept -- just a row counter).
SENTINELS: dict[str, str | None] = {c: f"SENTINEL-{re.sub(r'[^A-Z0-9]+', '-', c.upper()).strip('-')}"
                                    for c in import_presets.HEADER_COLUMNS}
for _safe in ("Sr.No", "Program Name", "Stream", "Student First Name", "Student Middle Name",
             "Student Last Name", "PRN No.", "Email Id", "Mobile No."):
    SENTINELS[_safe] = None
assert set(SENTINELS) == set(import_presets.HEADER_COLUMNS)


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


def tag():
    return uuid.uuid4().hex[:10].upper()


# --------------------------------------------------------------------------------------- fixture building
def _row(*, prn, first="", middle="", last="", programme="B. Tech. Computer Science", school="Engineering",
         email="", mobile="", sr_no=1):
    """One 39-cell data row in `import_presets.HEADER_COLUMNS` order. Every column this preset must
    never map gets its fixed sentinel value; the six safe ones (plus Sr.No) get exactly what the
    caller asked for."""
    values = {
        "Sr.No": sr_no, "Program Name": programme, "Stream": school,
        "Student First Name": first, "Student Middle Name": middle, "Student Last Name": last,
        "PRN No.": prn, "Email Id": email, "Mobile No.": mobile,
    }
    for column, sentinel in SENTINELS.items():
        if column not in values:
            values[column] = sentinel
    return [values[c] for c in import_presets.HEADER_COLUMNS]


def xls_bytes(data_rows: list[list], *, junk_rows_above=4) -> bytes:
    """A genuine legacy .xls (BIFF) workbook: `junk_rows_above` rows of report boilerplate, then the
    39-column header, then the data. Written with xlwt so the round trip through xlrd -- the engine
    a real .xls upload actually needs -- is real, not simulated."""
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("Report")
    boilerplate = ["Student Convocation Detail Report", "", "As on 2026-09-22", ""]
    for r in range(junk_rows_above):
        if r < len(boilerplate) and boilerplate[r]:
            sheet.write(r, 0, boilerplate[r])
    header_row = junk_rows_above
    for c, name in enumerate(import_presets.HEADER_COLUMNS):
        sheet.write(header_row, c, name)
    for r, row in enumerate(data_rows, start=header_row + 1):
        for c, value in enumerate(row):
            if value is not None and value != "":
                sheet.write(r, c, value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


LONGEST_NAME = "Almas Annam Khirkhani Turab Ali Khan Khan"          # 41 characters, real, PRN 202210101701
LONGEST_PROGRAMME = ("B. Tech. Computer Science and Engineering (IoT, Cyber Security "
                     "Including Block Chain Technology)")            # 96 characters, real, PRN 202256108001


def sample_fixture_rows(*, duplicate_prn="202308116012"):
    """Every quirk asked for, in one small fixture: a middle name, no middle name, a blank email,
    the real duplicate-PRN quirk (twice, with a DIFFERENT Sr.No -- exactly how it appears in the
    real file: the report's own running number differs, everything that matters does not), and the
    two longest strings, given verbatim."""
    return [
        _row(prn=f"{tag()}", first="Riya", middle="Anjali", last="Deshmukh", email="riya@example.test",
            mobile="9876500001", sr_no=1),
        _row(prn=f"{tag()}", first="Karan", last="Mehta", email="karan@example.test",
            mobile="9876500002", sr_no=2),                                      # no middle name
        _row(prn=f"{tag()}", first="Sana", last="Sheikh", email="", mobile="9876500003", sr_no=3),  # blank email
        _row(prn=duplicate_prn, first="Pooja", last="Kulkarni", email="pooja@example.test",
            mobile="9876500004", sr_no=4),
        _row(prn=duplicate_prn, first="Pooja", last="Kulkarni", email="pooja@example.test",
            mobile="9876500004", sr_no=5),                                      # exact repeat, DIFFERENT Sr.No
        _row(prn=f"{tag()}", first="Almas", middle="Annam Khirkhani Turab Ali", last="Khan Khan",
            email="almas@example.test", mobile="9876500006", sr_no=6),
        _row(prn=f"{tag()}", first="Devendra", last="Patil", programme=LONGEST_PROGRAMME,
            email="devendra@example.test", mobile="9876500007", sr_no=7),
    ]


def assert_no_sentinel(*blobs) -> None:
    for blob in blobs:
        text_blob = blob if isinstance(blob, str) else repr(blob)
        assert SENTINEL_PREFIX not in text_blob, f"a redacted field leaked: {text_blob[:4000]}"


# ============================================================================ DETECTION
class TestPresetDetection:
    def test_detects_the_header_wherever_it_actually_is(self):
        for junk in (0, 1, 4, 7):
            raw = xls_bytes(sample_fixture_rows(), junk_rows_above=junk)
            preset, header_row = import_presets.detect_preset(raw, engine="xlrd")
            assert preset is import_presets.UNIVERSITY_CONVOCATION_DETAIL_REPORT
            assert header_row == junk

    def test_is_robust_to_case_and_whitespace_in_the_header_cells(self):
        rows_ = sample_fixture_rows()
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Report")
        for c, name in enumerate(import_presets.HEADER_COLUMNS):
            sheet.write(4, c, f"  {name.upper()}  ")
        for r, row in enumerate(rows_, start=5):
            for c, value in enumerate(row):
                if value not in (None, ""):
                    sheet.write(r, c, value)
        buffer = io.BytesIO()
        workbook.save(buffer)
        preset, header_row = import_presets.detect_preset(buffer.getvalue(), engine="xlrd")
        assert preset is import_presets.UNIVERSITY_CONVOCATION_DETAIL_REPORT and header_row == 4

    def test_a_file_missing_one_expected_column_does_not_match(self):
        """A file missing so much as one of the 39 expected columns is probably not really this
        export -- matching it anyway would be a wrong-mapping risk, not a safety win."""
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Report")
        short_header = [c for c in import_presets.HEADER_COLUMNS if c != "Aadhar Card No"]
        for c, name in enumerate(short_header):
            sheet.write(0, c, name)
        buffer = io.BytesIO()
        workbook.save(buffer)
        preset, header_row = import_presets.detect_preset(buffer.getvalue(), engine="xlrd")
        assert preset is None and header_row is None

    def test_an_unrelated_spreadsheet_does_not_match(self):
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Report")
        for c, name in enumerate(["PRN", "Name", "Programme", "School"]):
            sheet.write(0, c, name)
        buffer = io.BytesIO()
        workbook.save(buffer)
        preset, header_row = import_presets.detect_preset(buffer.getvalue(), engine="xlrd")
        assert preset is None and header_row is None

    def test_the_never_map_guard_actually_names_every_column_the_hard_rule_lists(self):
        """A regression guard on the guard itself: every column the request explicitly forbade is on
        the never-map list, so a future edit to field_map that reintroduces one fails loudly at
        import time, not silently at run time."""
        hard_rule_columns = [
            "Aadhar Card No", "Address", "Guest Name", "Guest Gender", "Guest Relation",
            "Payment Details", "Courier Address",
            "Student First Name In Marathi", "Student Middle Name In Marathi",
            "Student Last Name In Marathi", "Student Mother Name In Marathi", "Student Father Name In Marathi",
            "Alternate Email Id", "Alternate Mobile No.",
            "CGPA", "Last Exam Grade", "Last Exam Year", "Batch Name",
            "Date of Admission", "Form Submission Date", "Certificate By Courier",
        ]
        from backend.import_presets import _MUST_NEVER_BE_MAPPED, _normalise
        for column in hard_rule_columns:
            assert _normalise(column) in _MUST_NEVER_BE_MAPPED, column


# ============================================================================ THE ALLOWLIST PROJECTION
class TestApplyPresetRedaction:
    def _parsed(self, junk_rows_above=4):
        raw = xls_bytes(sample_fixture_rows(), junk_rows_above=junk_rows_above)
        preset, header_row = import_presets.detect_preset(raw, engine="xlrd")
        assert preset is not None
        df = pd.read_excel(io.BytesIO(raw), engine="xlrd", header=header_row, dtype=str)
        return import_presets.apply_preset(preset, df)

    def test_the_returned_columns_are_only_the_six_safe_fields_plus_status(self):
        columns, _ = self._parsed()
        assert set(columns) == {"prn", "name", "programme", "school", "email", "mobile", "status"}

    def test_not_one_row_dict_contains_any_denylisted_key_or_value(self):
        _, record_rows = self._parsed()
        assert len(record_rows) == len(sample_fixture_rows())
        for row in record_rows:
            assert set(row) == {"prn", "name", "programme", "school", "email", "mobile", "status"}
            assert_no_sentinel(row)

    def test_a_middle_name_is_joined_with_single_spaces(self):
        _, record_rows = self._parsed()
        riya = next(r for r in record_rows if r["email"] == "riya@example.test")
        assert riya["name"] == "Riya Anjali Deshmukh"

    def test_no_middle_name_gives_no_double_space(self):
        _, record_rows = self._parsed()
        karan = next(r for r in record_rows if r["email"] == "karan@example.test")
        assert karan["name"] == "Karan Mehta"
        assert "  " not in karan["name"]

    def test_a_blank_email_cell_is_none_not_a_blank_string_or_the_word_nan(self):
        _, record_rows = self._parsed()
        sana = next(r for r in record_rows if r["name"] == "Sana Sheikh")
        assert sana["email"] is None

    def test_the_longest_name_is_reconstructed_exactly(self):
        _, record_rows = self._parsed()
        almas = next(r for r in record_rows if r["email"] == "almas@example.test")
        assert almas["name"] == LONGEST_NAME
        assert len(almas["name"]) == 41

    def test_the_longest_programme_is_kept_exactly(self):
        _, record_rows = self._parsed()
        devendra = next(r for r in record_rows if r["email"] == "devendra@example.test")
        assert devendra["programme"] == LONGEST_PROGRAMME
        assert len(devendra["programme"]) == 96

    def test_status_is_forced_active_for_every_row_the_file_has_no_status_column_at_all(self):
        _, record_rows = self._parsed()
        assert all(r["status"] == "ACTIVE" for r in record_rows)

    def test_the_preset_only_fires_on_the_literal_header_it_expects(self):
        """If the real file's header text differs from the reconstruction even slightly (a wording
        difference normalisation cannot absorb), the preset does not match and the file falls back
        to the ordinary manual mapping screen -- nothing crashes, and this is the safe default: an
        unrecognised file's columns are supposed to be reviewable by a person."""
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Report")
        reworded = [c.replace("Program Name", "Programme Name") for c in import_presets.HEADER_COLUMNS]
        for c, name in enumerate(reworded):
            sheet.write(0, c, name)
        sheet.write(1, list(import_presets.HEADER_COLUMNS).index("Aadhar Card No"), "SENTINEL-AADHAR-CARD-NO")
        buffer = io.BytesIO()
        workbook.save(buffer)
        preset, header_row = import_presets.detect_preset(buffer.getvalue(), engine="xlrd")
        assert preset is None and header_row is None


# ============================================================================ .xls / .xlsx ENGINE SELECTION
class TestFileFormatSelection:
    def test_a_genuine_legacy_xls_file_is_read_without_error(self):
        """`parse_file` alone (the generic path, no preset knowledge) must not crash on a real .xls
        -- that is the bug this fix closes. It reads the boilerplate title row as the header here
        (it has no idea this file needs row 5), which is exactly why `read_and_validate` tries
        `detect_preset` first; correct extraction of the real data is proven separately, through
        `read_and_validate`, elsewhere in this module."""
        from backend.importer import parse_file
        raw = xls_bytes(sample_fixture_rows())
        columns, parsed_rows = parse_file(io.BytesIO(raw), "convocation_detail_report.xls")
        assert len(parsed_rows) > 0 and len(columns) > 0

    def test_xls_extension_is_read_with_the_xlrd_engine_not_openpyxl(self, monkeypatch):
        calls = []
        real_read_excel = pd.read_excel

        def spy(*args, **kwargs):
            calls.append(kwargs.get("engine"))
            return real_read_excel(*args, **kwargs)

        monkeypatch.setattr(pd, "read_excel", spy)
        from backend.importer import parse_file
        parse_file(io.BytesIO(xls_bytes(sample_fixture_rows())), "report.xls")
        assert calls and all(c == "xlrd" for c in calls), calls

    def test_xlsx_extension_is_still_read_with_openpyxl(self, monkeypatch):
        calls = []
        real_read_excel = pd.read_excel

        def spy(*args, **kwargs):
            calls.append(kwargs.get("engine"))
            return real_read_excel(*args, **kwargs)

        monkeypatch.setattr(pd, "read_excel", spy)
        from backend.importer import parse_file
        buf = io.BytesIO()
        pd.DataFrame([{"PRN": "X1", "Name": "A", "Programme": "B", "School": "C"}]).to_excel(
            buf, index=False, engine="openpyxl")
        parse_file(io.BytesIO(buf.getvalue()), "report.xlsx")
        assert calls and all(c == "openpyxl" for c in calls), calls

    def test_an_old_xls_upload_before_this_fix_would_have_crashed(self):
        """openpyxl cannot open the legacy BIFF format at all -- confirms the bug this fix closes
        was real, by trying the exact engine the importer used to hard-code for every Excel file."""
        raw = xls_bytes(sample_fixture_rows())
        with pytest.raises(Exception):
            pd.read_excel(io.BytesIO(raw), engine="openpyxl", dtype=str)

    def test_a_csv_upload_never_reaches_read_excel_at_all(self, monkeypatch):
        """detect_preset is only ever tried against a real Excel engine; a CSV upload must not even
        attempt pandas.read_excel (which would raise on CSV bytes and be silently swallowed)."""
        calls = []
        real_read_excel = pd.read_excel
        monkeypatch.setattr(pd, "read_excel", lambda *a, **k: (calls.append(1), real_read_excel(*a, **k))[1])
        from backend.importer import parse_file
        parse_file(io.BytesIO(b"PRN,Name,Programme,School\nA1,A,B,C\n"), "students.csv")
        assert calls == []


# ============================================================================ EXACT vs CONFLICTING DUPLICATES
class TestExactDuplicateRowIsASkipNotAnError:
    """The behaviour change the real file's own quirk (PRN 202308116012, twice, identical) needed,
    tested directly against `validate_import` -- this applies to any import, preset or not."""

    MAPPING = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school"}

    def test_two_rows_that_agree_on_everything_mapped_is_a_skip_not_an_error(self, engine):
        prn = f"EXACT{tag()}"
        rows_in = [
            {"PRN": prn, "Name": "Pooja Kulkarni", "Programme": "B.Sc", "School": "Science"},
            {"PRN": prn, "Name": "Pooja Kulkarni", "Programme": "B.Sc", "School": "Science"},
        ]
        with engine.connect() as conn:
            preview = validate_import(rows_in, self.MAPPING, conn)
        assert preview.is_valid is True, [e.message for e in preview.errors]
        assert len(preview.to_create) == 1 and len(preview.to_skip) == 1
        assert preview.to_skip[0]["row"] == 2
        assert any("repeats row 1 exactly" in w.message for w in preview.warnings)
        assert not any(e.field == "prn" for e in preview.errors)

    def test_three_identical_rows_only_the_first_is_created(self, engine):
        prn = f"TRIPLE{tag()}"
        row = {"PRN": prn, "Name": "Same Student", "Programme": "B.A", "School": "Arts"}
        with engine.connect() as conn:
            preview = validate_import([dict(row), dict(row), dict(row)], self.MAPPING, conn)
        assert preview.is_valid is True
        assert len(preview.to_create) == 1 and len(preview.to_skip) == 2

    def test_two_rows_with_the_same_prn_but_different_data_is_still_a_fatal_error(self, engine):
        prn = f"CONFLICT{tag()}"
        rows_in = [
            {"PRN": prn, "Name": "Alice", "Programme": "CS", "School": "Eng"},
            {"PRN": prn, "Name": "Bob", "Programme": "CS", "School": "Eng"},   # same PRN, different name
        ]
        with engine.connect() as conn:
            preview = validate_import(rows_in, self.MAPPING, conn)
            assert preview.is_valid is False
            assert any(e.field == "prn" and "different data" in e.message for e in preview.errors)
            # is_valid False is what actually matters: it blocks the WHOLE commit, whatever
            # incidentally ended up in to_create (the first, non-conflicting occurrence legitimately
            # sits there; it is never written because commit_import refuses the whole preview).
            with pytest.raises(ValueError):
                commit_import(preview, conn)
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = :p", p=prn) == 0

    def test_a_committed_exact_duplicate_creates_only_one_student(self, engine):
        prn = f"COMMIT{tag()}"
        row = {"PRN": prn, "Name": "Once Only", "Programme": "B.Com", "School": "Commerce"}
        with engine.connect() as conn:
            preview = validate_import([dict(row), dict(row)], self.MAPPING, conn)
            summary = commit_import(preview, conn)
        assert summary.created == 1 and summary.skipped == 1
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = :p", p=prn) == 1

    def test_the_screen_commits_a_file_with_an_exact_duplicate_row(self, apps):
        """Same behaviour, driven through the real HTTP screen with a plain CSV -- this is not
        preset-specific."""
        client = admin(apps, "college")
        prn = f"SCREEN{tag()}"
        records = [
            {"PRN": prn, "Student Name": "Same Person", "Programme": "B.Tech", "School": "Engineering"},
            {"PRN": f"OTHER{tag()}", "Student Name": "Different Person", "Programme": "B.Tech", "School": "Engineering"},
            {"PRN": prn, "Student Name": "Same Person", "Programme": "B.Tech", "School": "Engineering"},
        ]
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        preview = preview_page(client, batch)
        assert 'data-preview="to_create">2<' in preview
        assert 'data-preview="to_skip">1<' in preview
        assert 'data-preview="errors">0<' in preview
        assert f"/admin/import/{batch}/commit" in preview
        response = commit_batch(client, batch)
        assert response.status_code == 303
        summary = numbers(summary_page(client, batch))
        assert summary == {"read": 3, "created": 2, "updated": 0, "skipped": 1, "errors": 0}


# ============================================================================ EMAIL / MOBILE
class TestEmailAndMobile:
    def test_email_and_mobile_are_stored_when_present(self, engine):
        prn = f"CONTACT{tag()}"
        rows_in = [{"PRN": prn, "Name": "Has Contact", "Programme": "B.Sc", "School": "Science",
                   "Email": "student@example.test", "Mobile": "9876543210"}]
        mapping = {**TestExactDuplicateRowIsASkipNotAnError.MAPPING, "Email": "email", "Mobile": "mobile"}
        with engine.connect() as conn:
            preview = validate_import(rows_in, mapping, conn)
            commit_import(preview, conn)
        row = rows(engine, "SELECT email, mobile FROM students WHERE prn = :p", p=prn)[0]
        assert row == {"email": "student@example.test", "mobile": "9876543210"}

    def test_a_blank_email_is_allowed_and_stored_as_null(self, engine):
        # A real upload always goes through `parse_file` first, which normalises every blank cell
        # to None before `validate_import` ever sees a row -- so None, not "", is what a blank cell
        # actually looks like by the time it gets here; that normalisation is exercised separately
        # (`test_a_blank_email_cell_is_none_not_a_blank_string_or_the_word_nan`, and the real-file
        # tests below, which never hand-build a row at all).
        prn = f"NOEMAIL{tag()}"
        rows_in = [{"PRN": prn, "Name": "No Contact", "Programme": "B.Sc", "School": "Science",
                   "Email": None, "Mobile": "9876543211"}]
        mapping = {**TestExactDuplicateRowIsASkipNotAnError.MAPPING, "Email": "email", "Mobile": "mobile"}
        with engine.connect() as conn:
            preview = validate_import(rows_in, mapping, conn)
            assert preview.is_valid is True
            commit_import(preview, conn)
        assert scalar(engine, "SELECT email FROM students WHERE prn = :p", p=prn) is None

    def test_email_and_mobile_are_not_required_to_import_at_all(self, engine):
        """No email/mobile column mapped at all -- still imports cleanly; nothing here is required."""
        prn = f"NOCOL{tag()}"
        rows_in = [{"PRN": prn, "Name": "No Column", "Programme": "B.Sc", "School": "Science"}]
        with engine.connect() as conn:
            preview = validate_import(rows_in, TestExactDuplicateRowIsASkipNotAnError.MAPPING, conn)
            assert preview.is_valid is True
            commit_import(preview, conn)
        row = rows(engine, "SELECT email, mobile FROM students WHERE prn = :p", p=prn)[0]
        assert row == {"email": None, "mobile": None}

    def test_a_frozen_students_email_is_locked_like_every_other_master_field(self, engine):
        """Migration 0011 added email/mobile to the same frozen-data guard as everything else."""
        from sqlalchemy.exc import DBAPIError

        from backend.snapshot import freeze_display_data
        prn = f"FROZEN{tag()}"
        rows_in = [{"PRN": prn, "Name": "Frozen Person", "Programme": "B.Sc", "School": "Science",
                   "Email": "before@example.test"}]
        mapping = {**TestExactDuplicateRowIsASkipNotAnError.MAPPING, "Email": "email"}
        with engine.connect() as conn:
            preview = validate_import(rows_in, mapping, conn)
            commit_import(preview, conn)
            freeze_display_data(conn)
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text("UPDATE students SET email = 'after@example.test' WHERE prn = :p"), {"p": prn})
        assert scalar(engine, "SELECT email FROM students WHERE prn = :p", p=prn) == "before@example.test"


# ============================================================================ AGAINST THE REAL FILE
REAL_FILE = pathlib.Path(__file__).resolve().parent.parent / "StudentConvocationDetailReport_Fees Paid Student.xls"


@pytest.mark.skipif(not REAL_FILE.exists(), reason="the real attached file is not present on this machine "
                                                    "(it is real students' data and is gitignored on purpose)")
class TestAgainstTheRealFile:
    """Everything above proves the mechanism with a synthetic fixture; this proves it against the
    actual attached export. Skipped, not failed, when the file is not there -- see REAL_FILE above
    and .gitignore: this file must never be committed."""

    @classmethod
    @pytest.fixture(scope="class")
    def real_bytes(cls):
        return REAL_FILE.read_bytes()

    def test_the_preset_matches_the_real_file(self, real_bytes):
        preset, header_row = import_presets.detect_preset(real_bytes, engine="xlrd")
        assert preset is import_presets.UNIVERSITY_CONVOCATION_DETAIL_REPORT
        assert header_row == 4

    def test_the_real_file_previews_at_exactly_the_numbers_asked_for(self, engine, real_bytes):
        cols, parsed_rows, mapping, preview = read_and_validate(
            engine, real_bytes, "StudentConvocationDetailReport_Fees Paid Student.xls")
        assert set(cols) == {"prn", "name", "programme", "school", "email", "mobile", "status"}
        assert preview.is_valid is True, [e.message for e in preview.errors][:5]
        assert len(preview.to_create) == 1200
        assert len(preview.to_skip) == 1
        assert len(preview.errors) == 0
        assert any("202308116012" in w.prn and "repeats row" in w.message for w in preview.warnings)

    def test_no_row_dict_contains_anything_from_a_denylisted_column(self, real_bytes):
        preset, header_row = import_presets.detect_preset(real_bytes, engine="xlrd")
        df = pd.read_excel(io.BytesIO(real_bytes), engine="xlrd", header=header_row, dtype=str)
        cols, parsed_rows = import_presets.apply_preset(preset, df)
        assert set(cols) == {"prn", "name", "programme", "school", "email", "mobile", "status"}
        for row in parsed_rows:
            assert set(row) == {"prn", "name", "programme", "school", "email", "mobile", "status"}

    def test_the_real_file_imports_successfully_through_the_actual_upload_screen(self, apps, engine, real_bytes):
        """Everything else in this class proves the mechanism by calling `read_and_validate` /
        `commit_import` directly; this drives the exact HTTP path an Admin uses in production --
        upload, match columns, preview, commit, summary -- with the real attached file end to end.
        Cleans up its own 1200 committed students afterwards so it does not shift the counts the
        other tests in this class expect."""
        client = admin(apps, "college")
        response = upload(client, real_bytes, filename="StudentConvocationDetailReport_Fees Paid Student.xls")
        batch = batch_of(response)
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        preview = preview_page(client, batch)
        assert 'data-preview="to_create">1200<' in preview
        assert 'data-preview="to_skip">1<' in preview
        assert 'data-preview="errors">0<' in preview
        response = commit_batch(client, batch)
        assert response.status_code == 303
        summary = numbers(summary_page(client, batch))
        assert summary == {"read": 1201, "created": 1200, "updated": 0, "skipped": 1, "errors": 0}

        real_prns = [str(p) for p in pd.read_excel(io.BytesIO(real_bytes), engine="xlrd", header=4,
                                                    dtype=str)["PRN No."].tolist()]
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM students WHERE prn = ANY(:p)"), {"p": real_prns})

    def test_committing_the_real_file_gives_1201_read_1200_created_1_skipped(self, engine, real_bytes):
        # This test module shares one Postgres database across every test in it (the project's own
        # convention: see the `engine` fixture in tests/test_station_engine.py), so counts here are
        # scoped to the real file's own PRNs, not a bare `count(*)` -- other tests in this module
        # legitimately add their own, differently-tagged students to the same table.
        real_prns = [str(p) for p in pd.read_excel(io.BytesIO(real_bytes), engine="xlrd", header=4,
                                                    dtype=str)["PRN No."].tolist()]
        _, _, _, preview = read_and_validate(engine, real_bytes, "real.xls")
        with engine.connect() as conn:
            summary = commit_import(preview, conn, filename="real.xls")
        assert (summary.read, summary.created, summary.skipped, summary.errors) == (1201, 1200, 1, 0)
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=real_prns) == 1200
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = '202308116012'") == 1

    def test_the_whole_database_has_no_trace_of_any_denylisted_column(self, engine, real_bytes):
        """Grep every text-ish column of every table for real, distinctive values pulled straight
        from the denylisted columns of the real file (values under 6 characters are skipped: too
        short to be a meaningful fingerprint, and prone to matching ordinary data by coincidence)."""
        _, _, _, preview = read_and_validate(engine, real_bytes, "real.xls")
        with engine.connect() as conn:
            commit_import(preview, conn)

        df = pd.read_excel(io.BytesIO(real_bytes), engine="xlrd", header=4, dtype=str)
        denylisted = [c for c, s in SENTINELS.items() if s is not None]
        # Two columns are excluded from the VALUE grep specifically (the schema-level and code-level
        # checks above still cover them completely): this university reuses the same number for PRN
        # and "Enrollment No/Roll No" for some students, and a father's given name is, by a common
        # naming convention in the real data, sometimes also the child's own middle name -- both are
        # genuine coincidental overlaps with an ALLOWED field's legitimate content, not the denied
        # column's data leaking, and were confirmed by hand against this exact file before this test
        # was written.
        denylisted = [c for c in denylisted if c not in ("Enrollment No/Roll No", "Student Mother Name",
                                                          "Student Father Name")]
        markers = []
        for column in denylisted:
            for value in df[column].dropna():
                text_value = str(value).strip()
                if len(text_value) >= 6:
                    markers.append(text_value)
                    break

        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
            )).fetchall()]
            for table in tables:
                text_cols = [r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=:t AND data_type IN ('text','character varying','character','jsonb','json')"
                ), {"t": table}).fetchall()]
                for col in text_cols:
                    for marker in markers:
                        found = conn.execute(text(f'SELECT count(*) FROM "{table}" WHERE "{col}"::text ILIKE :p'),
                                             {"p": f"%{marker}%"}).scalar()
                        # A hit is only real if the VALUE actually came from that denylisted column for
                        # that column's own field, not a coincidental overlap with an allowed field
                        # (e.g. this university reuses the PRN as the Enrollment No for some students,
                        # and a father's given name is sometimes also the child's own middle name).
                        assert not found, (table, col, marker, found)

    def test_the_longest_name_and_programme_pass_render_with_no_truncation(self, engine, real_bytes):
        from backend import passes, qr_tokens
        _, _, _, preview = read_and_validate(engine, real_bytes, "real.xls")
        with engine.connect() as conn:
            commit_import(preview, conn)
        qr_tokens.generate_missing_tokens(engine)
        with engine.connect() as conn:
            for prn, expected_len in (("202210101701", 41), ("202256108001", 96)):
                sid = conn.execute(text("SELECT id FROM students WHERE prn = :p"), {"p": prn}).scalar_one()
                data = passes.to_pass_data(passes.load_passes(conn, student_id=str(sid)))[0]
                assert len(data.name if expected_len == 41 else data.programme) == expected_len
                result = passes.render_single(data, "Annual Convocation 2026")
                assert result.pdf.startswith(b"%PDF")

"""A saved import mapping preset for the university's real "Student Convocation Detail Report"
export (.xls, header on the physical 5th row, 39 columns), and the two behaviour changes it needed:

  1. THE HARD RULE. Aadhar number, address, guardian details, payment details, courier address,
     the Marathi name fields, alternate contact details, CGPA, last-exam grade/year, batch name,
     date of admission, form submission date and "certificate by courier" must never be mapped,
     stored, logged or displayed -- anywhere, including logs and audit entries. This is enforced as
     an ALLOWLIST (`backend/import_presets.py`): a matched file is projected down to ONLY the six
     safe fields the whole way back at the parsing step, before a row becomes a Python dict any
     later code -- including the mapping screen's own "first values" sample -- could read. What is
     tested here is not "the screen hides it": it is "the value was never there to hide".

  2. A duplicate PRN that repeats an EARLIER ROW EXACTLY (the real file's own quirk: PRN
     202308116012 appears twice, byte-for-byte the same) is a normal duplicate-skip, like a student
     already on the list, and must not block the rest of the file. A duplicate PRN whose rows
     DISAGREE with each other is still a fatal error: nothing can guess which one is right.

Two things about the reconstructed header (`import_presets.HEADER_COLUMNS`): its exact wording was
never checked against a real export (none was attached to the request that asked for this), and the
grouping the request described ("the same 4 in Marathi") is one column short of mirroring every
English name field one-for-one. Both are called out again at the point they matter below, and
`test_the_preset_only_fires_on_the_literal_header_it_expects` documents exactly what happens if the
real file's wording differs even slightly: nothing crashes, nothing leaks -- the file simply falls
back to the ordinary, manual mapping screen, which is the deliberately safe default.
"""
from __future__ import annotations

import io
import re
import uuid

import pandas as pd
import pytest
import xlwt
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

pytest.importorskip("xlrd")

SENTINEL_PREFIX = "SENTINEL-"

# One fixed, unmistakable marker per denylisted column. Every sentinel shares the same prefix, so
# "does SENTINEL- appear anywhere at all, in any table, after the import" is one grep.
SENTINELS = {
    "Sr.No": None,  # numeric row counter; not sensitive, just not one of the six mapped fields
    "Signature": "SENTINEL-SIGNATURE-BLOCK",
    "Student Mother Name": "SENTINEL-MOTHER-NAME-Sunita",
    "Student Father Name": "SENTINEL-FATHER-NAME-Ramesh",
    "Student First Name In Marathi": "SENTINEL-MARATHI-FIRST",
    "Student Middle Name In Marathi": "SENTINEL-MARATHI-MIDDLE",
    "Student Last Name In Marathi": "SENTINEL-MARATHI-LAST",
    "Student Mother Name In Marathi": "SENTINEL-MARATHI-MOTHER",
    "Student Father Name In Marathi": "SENTINEL-MARATHI-FATHER",
    "Gender": "SENTINEL-GENDER-VALUE",
    "Enrollment No/Roll No": "SENTINEL-ENROLL-778899",
    "Date of Admission": "SENTINEL-ADMISSION-DATE-2022-07-01",
    "Aadhar Card No": "SENTINEL-AADHAR-471122889900",
    "Admission Type Category": "SENTINEL-ADMISSION-TYPE-REGULAR",
    "Address": "SENTINEL-ADDRESS-221B-Baker-Street",
    "Alternate Email Id": "SENTINEL-ALT-EMAIL-xyz@sentinel.test",
    "Alternate Mobile No.": "SENTINEL-ALT-MOBILE-9998887770",
    "Form Submission Date": "SENTINEL-FORM-DATE-2026-01-01",
    "Last Exam Name & Month": "SENTINEL-EXAM-NAME-MONTH",
    "Last Exam Year": "SENTINEL-EXAM-YEAR-2025",
    "Last Exam Grade": "SENTINEL-EXAM-GRADE-A-PLUS",
    "Convocation Presentia": "SENTINEL-PRESENTIA-YES",
    "Certificate By Courier": "SENTINEL-CERT-COURIER-YES",
    "Guest Name": "SENTINEL-GUEST-NAME-Uncle-Rajesh",
    "Guest Gender": "SENTINEL-GUEST-GENDER-M",
    "Guest Relation": "SENTINEL-GUEST-RELATION-Uncle",
    "Payment Details": "SENTINEL-PAYMENT-UPI-REF-9910",
    "Courier Address": "SENTINEL-COURIER-ADDRESS-Depot-5",
    "Batch Name": "SENTINEL-BATCH-2026-A",
    "CGPA": "SENTINEL-CGPA-9.87",
    "Result Declaration Date": "SENTINEL-RESULT-DATE-2025-06-01",
    "Result Declaration Date": "SENTINEL-RESULT-DATE-2025-06-01",
}
assert set(SENTINELS) | {"Program Name", "Stream", "PRN No.", "Student First Name",
                         "Student Middle Name", "Student Last Name", "Email Id", "Mobile No."} \
    == set(import_presets.HEADER_COLUMNS), "every header column needs either a sentinel or to be one of the six mapped fields"


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
    never map gets its fixed sentinel value; the six safe ones get exactly what the caller asked for."""
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
    39-column header, then the data. Written with xlwt so the round trip through `xlrd` -- the
    engine real .xls uploads actually need -- is real, not simulated."""
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


LONGEST_NAME = "Almas Annam Khirkhani Turab Ali Khan Khan"          # 41 characters, given verbatim
LONGEST_PROGRAMME = ("B. Tech. Computer Science and Engineering (IoT, Cyber Security "
                     "Including Block Chain Technology)")            # 96 characters, given verbatim


def sample_fixture_rows(*, duplicate_prn="202308116012"):
    """Every quirk asked for, in one small fixture: a middle name, no middle name, a blank email,
    the real duplicate-PRN quirk (twice, with a different Sr.No -- a repeated report row commonly
    keeps a fresh serial number even though the substance repeats), and the two longest strings."""
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
        raw = xls_bytes(rows_)
        # Corrupt the header row's casing/whitespace directly in the workbook and confirm it still matches.
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
        """A file that is missing so much as one of the 39 expected columns is probably not really
        this export -- matching it anyway would be a wrong-mapping risk, so it simply falls through."""
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
            "Student Last Name In Marathi", "Student Mother Name In Marathi",
            "Student Father Name In Marathi",
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
        difference normalisation cannot absorb), the preset does not match and the file falls back to
        the ordinary manual mapping screen -- nothing crashes, and this is the safe default, not a
        bug: an unrecognised file's columns are supposed to be reviewable by a person."""
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Report")
        reworded = [c.replace("Program Name", "Programme Name") for c in import_presets.HEADER_COLUMNS]
        for c, name in enumerate(reworded):
            sheet.write(0, c, name)
        sheet.write(1, 2, "SENTINEL-AADHAR-471122889900")  # what WOULD leak if this wrongly matched
        buffer = io.BytesIO()
        workbook.save(buffer)
        preset, header_row = import_presets.detect_preset(buffer.getvalue(), engine="xlrd")
        assert preset is None and header_row is None


# ============================================================================ .xls / .xlsx ENGINE SELECTION
class TestFileFormatSelection:
    def test_a_genuine_legacy_xls_file_is_read_without_error(self):
        from backend.importer import parse_file
        raw = xls_bytes(sample_fixture_rows(), junk_rows_above=0)
        columns, parsed_rows = parse_file(io.BytesIO(raw), "convocation_detail_report.xls")
        assert len(parsed_rows) == len(sample_fixture_rows())

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
        """openpyxl cannot open the legacy BIFF format at all -- confirms the bug this fix closes was
        real, using the same engine the importer used to hard-code."""
        raw = xls_bytes(sample_fixture_rows())
        with pytest.raises(Exception):
            pd.read_excel(io.BytesIO(raw), engine="openpyxl", dtype=str)

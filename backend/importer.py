"""backend/importer.py — Phase 3 incremental CSV/XLSX importer.

Rules (AGENTS.md golden rules, SYSTEM_SPEC.md section 25.11):
- Re-running with more rows NEVER duplicates or modifies existing students.
- Existing QR tokens are never touched.
- All validation errors are surfaced in a preview; a preview with errors blocks commit.
- The whole batch applies or none of it does (one database transaction).
- Import is logged to audit_log.
"""
from __future__ import annotations

import dataclasses
import io
import json
import logging
import math
import pathlib
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.faculty_map import Faculty, derive_faculty

MAX_NAME_LENGTH = 200  # characters; overlong if > this

# Which pandas engine reads which spreadsheet format. openpyxl only understands the modern,
# zip-based .xlsx/.xlsm container; a genuine legacy .xls (the OLE2/BIFF format most university
# systems still export, including Apache POI's HSSFWorkbook) needs xlrd instead — openpyxl raises
# on one immediately. Anything else is read as CSV.
_EXCEL_ENGINE_BY_EXTENSION = {".xlsx": "openpyxl", ".xlsm": "openpyxl", ".xls": "xlrd"}


def excel_engine_for(filename: str) -> Optional[str]:
    """The pandas `engine=` this filename's extension needs, or None if it is not an Excel file at all."""
    suffix = pathlib.Path((filename or "").lower()).suffix
    return _EXCEL_ENGINE_BY_EXTENSION.get(suffix)

# ──────────────────────────────────────────────────────────────────────────────
# Column aliases accepted from the university's file (case-insensitive, stripped)
# ──────────────────────────────────────────────────────────────────────────────
FIELD_LABELS = {
    "prn": "PRN / ID",
    "name": "Full Name",
    "first_name": "First Name",
    "middle_name": "Middle Name",
    "last_name": "Last Name",
    "programme": "Programme",
    "department": "Department",
    "stream": "Stream",
    "school": "School / Department",
    "enrollment_no": "Enrollment / Roll No",
    "sr_no": "Sr No.",
    "mother_name": "Mother's Name",
    "father_name": "Father's Name",
    "gender": "Gender",
    "sequence_no": "Sequence No",
    "seat_no": "Seat No",
    "awards": "Awards",
    "photo": "Photo",
    "status": "Record Status",
    "email": "Email",
    "mobile": "Mobile",
}
REQUIRED_FIELDS = ["prn", "name", "programme", "school"]


def check_required_fields_satisfied(mapped_canonicals: Iterable[str]) -> tuple[bool, list[str]]:
    """Check whether all logical required fields are satisfied by the mapped columns.
    
    - prn: satisfied by 'prn' or 'enrollment_no'
    - name: satisfied by 'name' or 'first_name'
    - programme: satisfied by 'programme'
    - school: satisfied by 'school' or 'department' or 'stream'
    """
    mapped = set(mapped_canonicals)
    missing = []
    if not ({"prn", "enrollment_no"} & mapped):
        missing.append(FIELD_LABELS["prn"])
    if not ({"name", "first_name"} & mapped):
        missing.append(FIELD_LABELS["name"])
    if "programme" not in mapped:
        missing.append(FIELD_LABELS["programme"])
    if not ({"school", "department", "stream"} & mapped):
        missing.append(FIELD_LABELS["school"])
    return len(missing) == 0, missing


def normalize_key(text: Any) -> str:
    """Normalize string for alias comparison: lowercase, replace punctuation with spaces, collapse spaces."""
    if text is None:
        return ""
    s = re.sub(r"[._/\-]+", " ", str(text).strip().lower())
    return " ".join(s.split())


_COLUMN_ALIASES: dict[str, list[str]] = {
    "prn": [
        "prn", "prn no", "prn no.", "prnno", "prn number", "student prn", "student_prn",
    ],
    "enrollment_no": [
        "enrollment no/roll no", "enrollment no / roll no", "enrollment no roll no",
        "enrollment no", "enrollment no.", "enrollment number",
        "roll no", "roll no.", "roll number", "enrollment", "rollno", "enrollment_no",
    ],
    "first_name": [
        "student first name", "first name", "firstname", "student_first_name",
        "student first name in english",
    ],
    "middle_name": [
        "student middle name", "middle name", "middlename", "student_middle_name",
        "student middle name in english",
    ],
    "last_name": [
        "student last name", "last name", "lastname", "surname", "student_last_name",
        "student last name in english",
    ],
    "name": [
        "name", "student name", "full name", "student full name",
        "studentname", "student_name",
    ],
    "programme": [
        "programme name", "program name", "programme", "program", "degree",
        "programme/degree", "programme / degree", "degree programme",
        "programme_degree", "course",
    ],
    "department": [
        "department name", "department", "dept name", "dept", "department_name",
    ],
    "stream": [
        "stream",
    ],
    "school": [
        "school", "school/department", "school / department", "school_department", "faculty",
    ],
    "sr_no": [
        "sr no.", "sr no", "sr.no.", "sr.no", "serial no.", "serial no", "sno", "s.no.", "s.no", "sr_no",
    ],
    "mother_name": [
        "student mother name", "mother name", "mother's name", "mothername",
        "student_mother_name", "student mother name in english",
    ],
    "father_name": [
        "student father name", "father name", "father's name", "fathername",
        "student_father_name", "student father name in english",
    ],
    "gender": [
        "gender", "sex",
    ],
    "email": [
        "email", "email id", "email_id", "e-mail", "e-mail id", "email address", "student email",
    ],
    "mobile": [
        "mobile", "mobile no", "mobile no.", "mobile number", "phone", "phone number",
        "contact number", "contact no", "contact no.", "mobile_no",
    ],
    "sequence_no": [
        "sequence no", "sequence no.", "sequence number",
        "convocation sequence no", "convocation sequence no.",
        "convocation sequence number", "seq no", "seq no.", "seq",
        "sequence_no", "sequenceno",
    ],
    "seat_no": [
        "seat no", "seat no.", "seat number", "seat",
        "seat_no", "seatno",
    ],
    "awards": [
        "awards", "award", "honours", "honors", "distinction",
        "medals", "prize",
    ],
    "photo": [
        "photo", "photo file", "photo path", "photo_path",
        "photograph", "image", "pic",
    ],
    "status": [
        "status", "record status", "student status",
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class ImportError:
    row: int
    field: str
    message: str


@dataclasses.dataclass
class ImportWarning:
    row: int
    field: str
    message: str
    prn: str | None = None


@dataclasses.dataclass
class FlaggedDuplicate:
    row: int
    prn: str
    type: str  # "in_file" | "existing_student"
    message: str


@dataclasses.dataclass
class ImportPreview:
    to_create: list[dict]         # rows that will be INSERTed
    to_skip: list[dict]           # rows whose PRN already exists in DB (skipped, NOT modified)
    errors: list[ImportError]     # fatal validation errors — blocks commit when non-empty
    flagged_duplicates: list[FlaggedDuplicate]
    warnings: list[ImportWarning] = dataclasses.field(default_factory=list)
    unmapped_programmes: list[str] = dataclasses.field(default_factory=list)
    unmapped_programmes_count: int = 0
    is_valid: bool = False


@dataclasses.dataclass
class ImportSummary:
    read: int
    created: int
    updated: int
    skipped: int
    errors: int
    unmapped_programmes: list[str] = dataclasses.field(default_factory=list)
    unmapped_programmes_count: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

RECOGNIZED_HEADER_PATTERNS: set[str] = set()
for _aliases in _COLUMN_ALIASES.values():
    for _a in _aliases:
        RECOGNIZED_HEADER_PATTERNS.add(normalize_key(_a))
for _other in (
    "signature", "date of admission", "aadhar card no", "admission type category",
    "address", "convocation presentia", "payment details", "batch name", "cgpa",
    "result declaration date", "last exam name", "last exam year", "last exam grade",
    "certificate by courier", "courier address", "guest name", "guest gender", "guest relation",
):
    RECOGNIZED_HEADER_PATTERNS.add(normalize_key(_other))


def detect_header_row(
    file_or_path: io.IOBase | pathlib.Path | str | bytes,
    filename: str,
    max_probe_rows: int = 15,
) -> int:
    """Scan the first ~10-15 rows and select the row containing the highest number of
    recognized student-related column names. Returns 0-indexed row number.
    """
    engine = excel_engine_for(filename)
    raw_source = file_or_path
    if isinstance(raw_source, bytes):
        raw_source = io.BytesIO(raw_source)
    elif isinstance(raw_source, (str, pathlib.Path)):
        raw_source = pathlib.Path(raw_source)

    pos = raw_source.tell() if hasattr(raw_source, "tell") else None

    try:
        if engine:
            probe = pd.read_excel(raw_source, engine=engine, header=None, nrows=max_probe_rows, dtype=str, keep_default_na=False)
        else:
            probe = pd.read_csv(raw_source, header=None, nrows=max_probe_rows, dtype=str, keep_default_na=False)
    except Exception as e:
        logger.debug("detect_header_row probe failed: %s", e)
        return 0
    finally:
        if pos is not None and hasattr(raw_source, "seek"):
            raw_source.seek(pos)

    best_row = 0
    max_score = 0

    for r_idx in range(len(probe)):
        row_vals = probe.iloc[r_idx].tolist()
        score = 0
        for val in row_vals:
            norm = normalize_key(val)
            if not norm:
                continue
            if norm in RECOGNIZED_HEADER_PATTERNS:
                score += 1
            else:
                for pat in RECOGNIZED_HEADER_PATTERNS:
                    if len(pat) >= 4 and (norm == pat or norm.startswith(pat) or pat in norm):
                        score += 1
                        break
        if score > max_score:
            max_score = score
            best_row = r_idx

    # Require at least 2 recognized headers to consider a non-row-0 as the header row
    if max_score >= 2:
        return best_row
    return 0


def parse_file(
    file_or_path: io.IOBase | pathlib.Path | str | bytes,
    filename: str,
    header_row: Optional[int] = None,
) -> tuple[list[str], list[dict]]:
    """Read a CSV or XLSX/XLS file and return (column_names, list_of_row_dicts).

    Automatically detects header row if header_row is None.
    Strips leading/trailing whitespace from all string values and column names.
    Empty strings are normalised to None.
    """
    raw_source = file_or_path
    if isinstance(raw_source, bytes):
        raw_source = io.BytesIO(raw_source)

    pos = raw_source.tell() if hasattr(raw_source, "tell") else None
    if header_row is None:
        header_row = detect_header_row(raw_source, filename)
        if pos is not None and hasattr(raw_source, "seek"):
            raw_source.seek(pos)

    engine = excel_engine_for(filename)
    if engine:
        df = pd.read_excel(raw_source, engine=engine, header=header_row, dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(raw_source, header=header_row, dtype=str, keep_default_na=False)

    # Normalise column names: strip whitespace
    df.columns = [str(c).strip() for c in df.columns]

    # Normalise values
    df = df.where(pd.notnull(df), None)
    for col in df.columns:
        df[col] = df[col].map(lambda v: v.strip() if isinstance(v, str) else v)
        df[col] = df[col].map(lambda v: None if v == "" else v)

    return list(df.columns), df.to_dict("records")


def detect_column_mapping(columns: list[str]) -> dict[str, str]:
    """Map raw file column names to canonical field names.

    Returns a dict {file_column -> canonical_field}.
    Only recognised columns are included.
    """
    mapping: dict[str, str] = {}
    mapped_canonicals: set[str] = set()

    for col in columns:
        if not col or col.startswith("Unnamed:"):
            continue
        norm = normalize_key(col)
        if not norm:
            continue
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical in mapped_canonicals:
                continue
            matched = False
            for alias in aliases:
                norm_alias = normalize_key(alias)
                if norm == norm_alias:
                    matched = True
                    break
            if matched:
                mapping[col] = canonical
                mapped_canonicals.add(canonical)
                break
    return mapping


def validate_import(
    rows: list[dict],
    column_mapping: dict[str, str],
    conn: Connection,
    photo_dir: str | None = None,
) -> ImportPreview:
    """Validate rows against the mapping and the current DB state.

    column_mapping may be either:
    - {file_col -> canonical}  (output of detect_column_mapping), OR
    - {canonical -> canonical} / {file_col -> canonical} mixed (used in tests
      where the dict keys already are the canonical names or the raw row dicts
      use canonical keys directly).

    A preview with errors has is_valid=False and must not be committed.
    """
    # Build a reverse lookup so we can read from row dicts using either form.
    # Determine whether rows already use canonical keys.
    canonical_fields = set(_COLUMN_ALIASES.keys())

    errors: list[ImportError] = []
    flagged_duplicates: list[FlaggedDuplicate] = []
    warnings: list[ImportWarning] = []
    to_create: list[dict] = []
    to_skip: list[dict] = []

    # Resolve per-row value using column_mapping
    def _get(row_dict: dict, canonical_field: str) -> Any:
        """Look up a canonical value from a row, trying both the mapped file
        column and the canonical name directly."""
        val = None
        for file_col, canon in column_mapping.items():
            if canon == canonical_field and file_col in row_dict:
                val = row_dict[file_col]
                break
        if val is None and canonical_field in row_dict:
            val = row_dict[canonical_field]

        if isinstance(val, float) and math.isnan(val):
            return None
        return val

    # Fetch all existing PRNs from DB once
    existing_prns: set[str] = {
        r[0] for r in conn.execute(text("SELECT prn FROM students")).fetchall()
    }
    existing_seq_nos: set[int] = {
        r[0] for r in conn.execute(text("SELECT sequence_no FROM students")).fetchall()
    }

    db_custom_map: dict[str, str] = {}
    db_overrides: set[str] = set()
    try:
        rows_pf = conn.execute(text("SELECT programme_key, faculty, coalesce(is_override, false) FROM programme_faculty")).fetchall()
        db_custom_map = {r[0]: r[1] for r in rows_pf}
        db_overrides = {r[0] for r in rows_pf if r[2]}
    except Exception:
        try:
            rows_pf = conn.execute(text("SELECT programme_key, faculty FROM programme_faculty")).fetchall()
            db_custom_map = {r[0]: r[1] for r in rows_pf}
        except Exception:
            pass
    unmapped_programmes_seen: set[str] = set()

    # Track duplicates within this file
    file_prns_seen: dict[str, int] = {}  # prn -> first row index (1-based)
    file_seqnos_seen: dict[int, int] = {}  # seq_no -> first row index

    REQUIRED = ["prn", "name", "programme", "school"]

    for idx, row in enumerate(rows, start=1):
        row_errors: list[ImportError] = []
        row_flags: list[FlaggedDuplicate] = []
        row_warnings: list[ImportWarning] = []

        # ── Required fields & flexible fallbacks ─────────────────────────────
        values: dict[str, Any] = {}

        prn_val = _get(row, "prn") or _get(row, "enrollment_no")
        if prn_val is None or (isinstance(prn_val, str) and prn_val.strip() == ""):
            row_errors.append(ImportError(row=idx, field="prn", message="Missing required field 'prn'"))
        else:
            values["prn"] = prn_val.strip() if isinstance(prn_val, str) else prn_val

        name_val = _get(row, "name")
        if name_val is None or (isinstance(name_val, str) and name_val.strip() == ""):
            parts = [
                str(p).strip() for p in (
                    _get(row, "first_name"),
                    _get(row, "middle_name"),
                    _get(row, "last_name"),
                ) if p is not None and str(p).strip() != ""
            ]
            if parts:
                name_val = " ".join(parts)
        if name_val is None or (isinstance(name_val, str) and name_val.strip() == ""):
            row_errors.append(ImportError(row=idx, field="name", message="Missing required field 'name'"))
        else:
            values["name"] = name_val.strip() if isinstance(name_val, str) else name_val

        prog_val = _get(row, "programme")
        if prog_val is None or (isinstance(prog_val, str) and prog_val.strip() == ""):
            row_errors.append(ImportError(row=idx, field="programme", message="Missing required field 'programme'"))
        else:
            values["programme"] = prog_val.strip() if isinstance(prog_val, str) else prog_val

        school_val = _get(row, "school") or _get(row, "department") or _get(row, "stream")
        if school_val is None or (isinstance(school_val, str) and school_val.strip() == ""):
            row_errors.append(ImportError(row=idx, field="school", message="Missing required field 'school'"))
        else:
            values["school"] = school_val.strip() if isinstance(school_val, str) else school_val

        # ── Sequence_no validation ───────────────────────────────────────────
        seq_raw = _get(row, "sequence_no")
        if seq_raw is not None and str(seq_raw).strip() != "":
            try:
                seq = int(seq_raw)
                if seq <= 0:
                    row_errors.append(ImportError(row=idx, field="sequence_no", message="sequence_no must be a positive integer"))
                    seq = None
            except (ValueError, TypeError):
                row_errors.append(ImportError(row=idx, field="sequence_no", message=f"sequence_no must be an integer, got '{seq_raw}'"))
                seq = None
            else:
                values["sequence_no"] = seq
        else:
            seq = None

        # ── Name length ──────────────────────────────────────────────────────
        if "name" in values and len(values["name"]) > MAX_NAME_LENGTH:
            row_errors.append(ImportError(
                row=idx, field="name",
                message=f"overlong name ({len(values['name'])} chars, max {MAX_NAME_LENGTH})",
            ))

        # ── Collect optional fields ──────────────────────────────────────────
        values["seat_no"] = _get(row, "seat_no")
        values["awards"] = _get(row, "awards")
        values["photo_path"] = _get(row, "photo")
        # Optional contact details.
        values["email"] = _get(row, "email")
        values["mobile"] = _get(row, "mobile")

        # University serial number
        sr_no_raw = _get(row, "sr_no")
        if sr_no_raw is not None and str(sr_no_raw).strip() != "":
            values["sr_no"] = str(sr_no_raw).strip()
        else:
            values["sr_no"] = None

        prn = values.get("prn")

        # Status has to be settled before the duplicate check just below: two rows that share a
        # PRN but disagree on whether the student is active are a real conflict, not a repeat.
        status = _get(row, "status")
        if status and str(status).strip().lower() == "inactive":
            row_warnings.append(ImportWarning(row=idx, field="status", prn=prn, message="Marked as inactive by the university, will be imported as inactive."))
            values["status"] = "INACTIVE"
        else:
            values["status"] = "ACTIVE"

        if not values.get("photo_path"):
            row_warnings.append(ImportWarning(row=idx, field="photo", prn=prn, message="No photo provided."))
        elif photo_dir:
            photo_file = pathlib.Path(photo_dir) / values["photo_path"]
            if not photo_file.exists():
                row_warnings.append(ImportWarning(row=idx, field="photo", prn=prn, message=f"Photo named '{values['photo_path']}' not found in upload directory."))

        # ── Within-file duplicate PRN ─────────────────────────────────────────
        # An EXACT repeat of an earlier row — same PRN AND every other student value the same — is the
        # export tool listing the same student twice (a re-run report, a pagination artefact: the
        # real university file does this with PRN 202308116012, same student, different Sr.No).
        # We exclude sr_no from the comparison so different report row counts on the same student
        # are treated as duplicates, preserving the first row's sr_no, not a conflict error.
        exact_repeat = False
        if prn:
            earlier = file_prns_seen.get(prn)
            comp_values = {k: v for k, v in values.items() if k != "sr_no"}
            if earlier is None:
                file_prns_seen[prn] = {"row": idx, "values": comp_values}
            elif earlier["values"] == comp_values:
                exact_repeat = True
                row_flags.append(FlaggedDuplicate(
                    row=idx, prn=prn, type="in_file",
                    message=f"Row {idx} repeats row {earlier['row']} exactly (also PRN '{prn}') — treated as a duplicate, not an error."))
                row_warnings.append(ImportWarning(
                    row=idx, field="prn", prn=prn,
                    message=f"This row repeats row {earlier['row']} exactly. Only the first is imported."))
            else:
                row_errors.append(ImportError(
                    row=idx, field="prn",
                    message=f"Duplicate PRN '{prn}' in file with different data (also at row {earlier['row']}) — cannot tell which is right",
                ))
                row_flags.append(FlaggedDuplicate(
                    row=idx, prn=prn, type="in_file",
                    message=f"Duplicate PRN in file at rows {earlier['row']} and {idx}, with different data"))

        # ── Within-file duplicate sequence_no check ──────────────────────────
        if seq is not None:
            if seq in file_seqnos_seen:
                row_warnings.append(ImportWarning(
                    row=idx, field="sequence_no", prn=prn,
                    message=f"Duplicate sequence number {seq} (also at row {file_seqnos_seen[seq]})"
                ))
            else:
                file_seqnos_seen[seq] = idx

        errors.extend(row_errors)
        flagged_duplicates.extend(row_flags)
        warnings.extend(row_warnings)

        if row_errors:
            continue

        if exact_repeat:
            to_skip.append({"row": idx, "prn": prn, **values})
            continue

        # ── Check against DB ─────────────────────────────────────────────────
        if prn and prn in existing_prns:
            fd = FlaggedDuplicate(row=idx, prn=prn, type="existing_student", message=f"PRN '{prn}' already exists in database (row {idx} skipped)")
            flagged_duplicates.append(fd)
            to_skip.append({"row": idx, "prn": prn, **values})
            continue

        if seq is not None and seq in existing_seq_nos:
            errors.append(ImportError(
                row=idx, field="sequence_no",
                message=f"sequence_no {seq} already exists in database",
            ))
            continue

        # Derive faculty:
        faculty = derive_faculty(
            school=values.get("school"),
            programme=values.get("programme"),
            custom_map=db_custom_map,
            overrides=db_overrides,
        )
        values["faculty"] = faculty
        if faculty == Faculty.UNMAPPED.value:
            prog_val = values.get("programme")
            if prog_val:
                unmapped_programmes_seen.add(prog_val.strip())

        to_create.append({"row": idx, **values})

    is_valid = len(errors) == 0
    unmapped_list = sorted(unmapped_programmes_seen)
    return ImportPreview(
        to_create=to_create,
        to_skip=to_skip,
        errors=errors,
        flagged_duplicates=flagged_duplicates,
        warnings=warnings,
        unmapped_programmes=unmapped_list,
        unmapped_programmes_count=len(unmapped_list),
        is_valid=is_valid,
    )


def commit_import(
    preview: ImportPreview,
    conn: Connection,
    operator_id: str | None = None,
    filename: str | None = None,
) -> ImportSummary:
    """Transactionally commit a validated import preview.

    Raises ValueError if preview.is_valid is False (zero writes guaranteed).
    """
    if not preview.is_valid:
        raise ValueError(
            f"Cannot commit an import with validation errors ({len(preview.errors)} error(s)). "
            "Check preview.errors before calling commit_import."
        )

    created = 0
    try:
        for r in preview.to_create:
            conn.execute(
                text(
                    """
                    INSERT INTO students (prn, name, programme, school, faculty,
                                         sequence_no, seat_no, awards, photo_path, status,
                                         email, mobile, sr_no)
                    VALUES (:prn, :name, :programme, :school, :faculty,
                            :sequence_no, :seat_no, :awards, :photo_path, :status,
                            :email, :mobile, :sr_no)
                    """
                ),
                {
                    "prn": r["prn"],
                    "name": r["name"],
                    "programme": r["programme"],
                    "school": r["school"],
                    "faculty": r.get("faculty", Faculty.UNMAPPED.value),
                    "sequence_no": r.get("sequence_no"),
                    "seat_no": r.get("seat_no"),
                    "awards": r.get("awards"),
                    "photo_path": r.get("photo_path"),
                    "status": r.get("status", "ACTIVE"),
                    "email": r.get("email"),
                    "mobile": r.get("mobile"),
                    "sr_no": r.get("sr_no"),
                },
            )
            created += 1

        total_read = len(preview.to_create) + len(preview.to_skip)
        summary = ImportSummary(
            read=total_read,
            created=created,
            updated=0,
            skipped=len(preview.to_skip),
            errors=len(preview.errors),
            unmapped_programmes=list(preview.unmapped_programmes),
            unmapped_programmes_count=preview.unmapped_programmes_count,
        )

        conn.execute(
            text(
                """
                INSERT INTO audit_log (action, details, operator_id)
                VALUES ('IMPORT_STUDENTS', CAST(:details AS jsonb), :operator_id)
                """
            ),
            {
                "details": json.dumps({
                    "read": summary.read,
                    "created": summary.created,
                    "updated": summary.updated,
                    "skipped": summary.skipped,
                    "errors": summary.errors,
                    "filename": filename,
                }),
                "operator_id": operator_id,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return summary
def read_and_validate(
    engine, content: bytes, filename: str, mapping: dict[str, str] | None = None,
    photo_dir: str | None = None
) -> tuple[list[str], list[dict], dict[str, str], ImportPreview]:
    """Parse, detect columns (or apply a saved preset), and validate all at once.

    Detection only ever runs against the file's OWN format's engine (`excel_engine_for`); a CSV is
    never handed to `pandas.read_excel` at all, and an Excel file is never mis-read with the wrong
    engine for its extension (openpyxl cannot open a legacy .xls; xlrd cannot open a modern .xlsx).
    """
    from backend.import_presets import apply_preset, detect_preset

    excel_engine = excel_engine_for(filename)
    preset, header_row = (detect_preset(content, engine=excel_engine) if excel_engine else (None, None))

    if preset:
        try:
            df = pd.read_excel(io.BytesIO(content), engine=excel_engine, header=header_row,
                               dtype=str, keep_default_na=False)
        except Exception as exc:
            raise ValueError(str(exc))
        cols, rows = apply_preset(preset, df)
        # For a preset, the mapping is implied (columns are already canonical): identity, always.
        mapping = {c: c for c in cols}
    else:
        try:
            cols, rows = parse_file(io.BytesIO(content), filename)
        except Exception as exc:
            raise ValueError(str(exc))
        mapping = mapping or detect_column_mapping(cols)

    with engine.begin() as conn:
        preview = validate_import(rows, mapping, conn, photo_dir)
        return cols, rows, mapping, preview

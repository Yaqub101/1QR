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
import math
import pathlib
from typing import Any, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

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
    "programme": "Programme",
    "school": "School / Department",
    "sequence_no": "Sequence No",
    "seat_no": "Seat No",
    "awards": "Awards",
    "photo": "Photo",
    "status": "Record Status",
    "email": "Email",
    "mobile": "Mobile",
}
REQUIRED_FIELDS = ["prn", "name", "programme", "school"]

_COLUMN_ALIASES: dict[str, list[str]] = {
    "prn": ["prn", "prnno", "prn no", "prn no.", "prn number", "student prn", "enrollment no", "enrollment number"],
    "name": [
        "name", "student name", "full name", "student full name",
        "studentname", "student_name",
    ],
    "programme": [
        "programme", "program", "degree", "programme/degree", "programme / degree",
        "degree programme", "programme_degree", "course",
    ],
    "school": [
        "school", "department", "school/department", "school / department",
        "dept", "faculty", "school_department",
    ],
    "sequence_no": [
        "sequence no", "sequence no.", "sequence number",
        "convocation sequence no", "convocation sequence no.",
        "convocation sequence number", "seq no", "seq no.", "seq",
        "sequence_no", "sequenceno", "sno",
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
        "status", "record status", "student status"
    ],
    "email": [
        "email", "email id", "e-mail", "e-mail id", "email address", "student email",
    ],
    "mobile": [
        "mobile", "mobile no", "mobile no.", "mobile number", "phone", "phone number",
        "contact number", "contact no", "contact no.",
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
    is_valid: bool = False


@dataclasses.dataclass
class ImportSummary:
    read: int
    created: int
    updated: int
    skipped: int
    errors: int


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def parse_file(
    file_or_path: io.IOBase | pathlib.Path | str,
    filename: str,
) -> tuple[list[str], list[dict]]:
    """Read a CSV or XLSX file and return (column_names, list_of_row_dicts).

    Strips leading/trailing whitespace from all string values and column names.
    Empty strings are normalised to None.
    """
    engine = excel_engine_for(filename)
    if engine:
        df = pd.read_excel(file_or_path, engine=engine, dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(file_or_path, dtype=str, keep_default_na=False)

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
    normalised = {c.lower().strip(): c for c in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                file_col = normalised[alias]
                if file_col not in mapping:  # first alias wins
                    mapping[file_col] = canonical
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

    # Track duplicates within this file
    file_prns_seen: dict[str, int] = {}  # prn -> first row index (1-based)
    file_seqnos_seen: dict[int, int] = {}  # seq_no -> first row index

    REQUIRED = ["prn", "name", "programme", "school"]

    for idx, row in enumerate(rows, start=1):
        row_errors: list[ImportError] = []
        row_flags: list[FlaggedDuplicate] = []
        row_warnings: list[ImportWarning] = []

        # ── Required fields ──────────────────────────────────────────────────
        values: dict[str, Any] = {}
        for field in REQUIRED:
            val = _get(row, field)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                row_errors.append(ImportError(row=idx, field=field, message=f"Missing required field '{field}'"))
            else:
                values[field] = val.strip() if isinstance(val, str) else val

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
        # Optional contact details. Many rows have no email at all — that is allowed, not an
        # error and not even a note: only a MISSING PHOTO gets a warning below, because a photo
        # affects the pass; a missing email or mobile affects nothing this system does today.
        values["email"] = _get(row, "email")
        values["mobile"] = _get(row, "mobile")

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
        # An EXACT repeat of an earlier row — same PRN AND every other value the same — is the
        # export tool listing the same student twice (a re-run report, a pagination artefact: the
        # real university file does this with PRN 202308116012, same student, different Sr.No).
        # That is a normal duplicate, exactly like a student already on the list: it is skipped,
        # noted, and does NOT block the rest of the file. A PRN that repeats with DIFFERENT data
        # is a genuine conflict — nothing here can guess which row is right — and still blocks.
        exact_repeat = False
        if prn:
            earlier = file_prns_seen.get(prn)
            if earlier is None:
                file_prns_seen[prn] = {"row": idx, "values": dict(values)}
            elif earlier["values"] == values:
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

        to_create.append({"row": idx, **values})

    is_valid = len(errors) == 0
    return ImportPreview(
        to_create=to_create,
        to_skip=to_skip,
        errors=errors,
        flagged_duplicates=flagged_duplicates,
        warnings=warnings,
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
                    INSERT INTO students (prn, name, programme, school,
                                         sequence_no, seat_no, awards, photo_path, status,
                                         email, mobile)
                    VALUES (:prn, :name, :programme, :school,
                            :sequence_no, :seat_no, :awards, :photo_path, :status,
                            :email, :mobile)
                    """
                ),
                {
                    "prn": r["prn"],
                    "name": r["name"],
                    "programme": r["programme"],
                    "school": r["school"],
                    "sequence_no": r.get("sequence_no"),
                    "seat_no": r.get("seat_no"),
                    "awards": r.get("awards"),
                    "photo_path": r.get("photo_path"),
                    "status": r.get("status", "ACTIVE"),
                    "email": r.get("email"),
                    "mobile": r.get("mobile"),
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

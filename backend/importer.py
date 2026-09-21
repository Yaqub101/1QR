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
import pathlib
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

MAX_NAME_LENGTH = 200  # characters; overlong if > this

# ──────────────────────────────────────────────────────────────────────────────
# Column aliases accepted from the university's file (case-insensitive, stripped)
# ──────────────────────────────────────────────────────────────────────────────
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
    is_valid: bool


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
    fname = filename.lower()
    if fname.endswith(".xlsx") or fname.endswith(".xls"):
        df = pd.read_excel(file_or_path, engine="openpyxl", dtype=str)
    else:
        df = pd.read_csv(file_or_path, dtype=str)

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
    to_create: list[dict] = []
    to_skip: list[dict] = []

    # Resolve per-row value using column_mapping
    def _get(row: dict, canonical: str):
        """Look up a canonical value from a row, trying both the mapped file
        column and the canonical name directly."""
        # First look for a file column that maps to this canonical
        for file_col, canon in column_mapping.items():
            if canon == canonical and file_col in row:
                return row[file_col]
        # Fallback: row may already use canonical keys
        if canonical in row:
            return row[canonical]
        return None

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

    REQUIRED = ["prn", "name", "programme", "school", "sequence_no"]

    for idx, row in enumerate(rows, start=1):
        row_errors: list[ImportError] = []
        row_flags: list[FlaggedDuplicate] = []

        # ── Required fields ──────────────────────────────────────────────────
        values: dict[str, Any] = {}
        for field in REQUIRED:
            val = _get(row, field)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                row_errors.append(ImportError(row=idx, field=field, message=f"Missing required field '{field}'"))
            else:
                values[field] = val.strip() if isinstance(val, str) else val

        # ── Sequence_no validation ───────────────────────────────────────────
        if "sequence_no" in values:
            try:
                seq = int(values["sequence_no"])
                if seq <= 0:
                    row_errors.append(ImportError(row=idx, field="sequence_no", message="sequence_no must be a positive integer"))
                    seq = None
            except (ValueError, TypeError):
                row_errors.append(ImportError(row=idx, field="sequence_no", message=f"sequence_no must be an integer, got '{values['sequence_no']}'"))
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

        # ── Within-file duplicate PRN check ──────────────────────────────────
        prn = values.get("prn")
        if prn:
            if prn in file_prns_seen:
                row_errors.append(ImportError(
                    row=idx, field="prn",
                    message=f"Duplicate PRN '{prn}' in file (also at row {file_prns_seen[prn]})",
                ))
                row_flags.append(FlaggedDuplicate(row=idx, prn=prn, type="in_file", message=f"Duplicate PRN in file at rows {file_prns_seen[prn]} and {idx}"))
            else:
                file_prns_seen[prn] = idx

        # ── Within-file duplicate sequence_no check ──────────────────────────
        if seq is not None:
            if seq in file_seqnos_seen:
                row_errors.append(ImportError(
                    row=idx, field="sequence_no",
                    message=f"Duplicate sequence_no {seq} in file (also at row {file_seqnos_seen[seq]})",
                ))
            else:
                file_seqnos_seen[seq] = idx

        errors.extend(row_errors)
        flagged_duplicates.extend(row_flags)

        if row_errors:
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
        is_valid=is_valid,
    )


def commit_import(
    preview: ImportPreview,
    conn: Connection,
    operator_id: str | None = None,
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
                                         sequence_no, seat_no, awards, photo_path)
                    VALUES (:prn, :name, :programme, :school,
                            :sequence_no, :seat_no, :awards, :photo_path)
                    """
                ),
                {
                    "prn": r["prn"],
                    "name": r["name"],
                    "programme": r["programme"],
                    "school": r["school"],
                    "sequence_no": r["sequence_no"],
                    "seat_no": r.get("seat_no"),
                    "awards": r.get("awards"),
                    "photo_path": r.get("photo_path"),
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
                INSERT INTO audit_log (action, details)
                VALUES ('IMPORT_STUDENTS', CAST(:details AS jsonb))
                """
            ),
            {
                "details": json.dumps({
                    "read": summary.read,
                    "created": summary.created,
                    "updated": summary.updated,
                    "skipped": summary.skipped,
                    "errors": summary.errors,
                })
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return summary

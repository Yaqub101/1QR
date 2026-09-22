"""Saved import mapping presets: known university export formats that auto-map themselves.

A preset exists for a source format someone has already looked at and vetted: it knows which
columns of that file are safe to bring into this system, and how to build each canonical field
from them. Everything else in the file — named here or not, sensitive or not, reviewed or not —
never becomes a value the rest of the import can see.

THIS IS AN ALLOWLIST, NOT A DENYLIST, and that is the point. A denylist has to name every
dangerous column and only stays correct for as long as nobody adds a new one to the export; an
allowlist keeps only what a person explicitly decided the ceremony needs, so a column nobody has
reviewed yet is dropped by default, not shown by default. `apply_preset` acts on the columns the
moment they leave pandas, before a single row becomes a Python dict the rest of the importer, the
mapping screen's sample, a log line or an audit entry could read — so a home address, a government
ID number, a guardian's details or a payment reference in a matched file is never mapped, never
stored, never logged and never displayed, anywhere, on purpose, not merely by omission downstream.

DETECTING THE HEADER ROW. A real export often has a title, a run date or a blank line above the
real header, and exactly which row that lands on varies release to release. Rather than trust one
fixed row number, `detect_preset` reads the first few rows with no header assumed at all and looks
for the row whose cells are (at least) every column a preset expects; whichever physical row that
turns out to be becomes the header. A file whose header text does not match closely enough (after
collapsing whitespace and case) simply does not match any preset and falls through to the ordinary,
general-purpose column-mapping screen — where an unfamiliar column's values ARE shown, because an
Admin reviewing a file nobody has vetted yet needs to see what is in it to map it correctly. Safety
here is a direct consequence of recognition: getting a preset's expected header text right is what
makes its hard rule bite.
"""
from __future__ import annotations

import dataclasses
import io
from typing import Any, Optional

import pandas as pd


def _normalise(value: Any) -> str:
    """Collapse whitespace and case, so a stray double space or a differently-cased header cell
    still matches. `None` / NaN normalise to the empty string, which never matches a real header."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return " ".join(str(value).strip().split()).casefold()


@dataclasses.dataclass(frozen=True)
class Preset:
    name: str
    # Every one of these (normalised) must be present among a row's cells for that row to be
    # accepted as this preset's header. This is deliberately the FULL known header, safe columns
    # and sensitive ones together, not just the columns being mapped: a file that is missing one of
    # the columns this preset expects is probably not actually this export, and treating it as one
    # anyway is a wrong-mapping risk, not a safety win.
    expected_columns: frozenset[str]
    # canonical field -> the raw column name(s) that build it (more than one is joined; see
    # `apply_preset`). This dict, and nothing else, decides what survives the import.
    field_map: dict[str, tuple[str, ...]]
    # canonical field -> a fixed value written for every row, when the file itself carries no such
    # column at all (for example: "this whole file is the paid-registrations list, so status is
    # always ACTIVE").
    constant_fields: dict[str, str] = dataclasses.field(default_factory=dict)

    def raw_columns_used(self) -> set[str]:
        used: set[str] = set()
        for sources in self.field_map.values():
            used.update(sources)
        return used


# --------------------------------------------------------------------------------------------
# The university's "Student Convocation Detail Report" (.xls export)
#
# Reconstructed from the description of the real file (39 columns, header on the physical 5th
# row). The literal header text has NOT been checked against a real export yet -- normalisation
# (whitespace + case) absorbs small cosmetic differences, but if a heading is worded differently in
# the real file this preset simply will not match it, and the file will fall back to the ordinary
# manual mapping screen. Update HEADER_COLUMNS below to the literal text the moment a real export
# is available; nothing else needs to change.
# --------------------------------------------------------------------------------------------
HEADER_COLUMNS: tuple[str, ...] = (
    "Sr.No", "Signature", "Program Name", "Stream",
    "Student First Name", "Student Middle Name", "Student Last Name",
    "Student Mother Name", "Student Father Name",
    "Student First Name In Marathi", "Student Middle Name In Marathi",
    "Student Last Name In Marathi", "Student Mother Name In Marathi",
    "Student Father Name In Marathi",
    "Gender", "PRN No.", "Enrollment No/Roll No", "Date of Admission",
    "Aadhar Card No", "Admission Type Category", "Address", "Email Id",
    "Alternate Email Id", "Mobile No.", "Alternate Mobile No.", "Form Submission Date",
    "Last Exam Name & Month", "Last Exam Year", "Last Exam Grade",
    "Convocation Presentia", "Certificate By Courier",
    "Guest Name", "Guest Gender", "Guest Relation",
    "Payment Details", "Courier Address", "Batch Name", "CGPA", "Result Declaration Date",
)
assert len(HEADER_COLUMNS) == 39, len(HEADER_COLUMNS)  # exactly what the real file was described as having

# Columns the HARD RULE names explicitly. Not the mechanism (that is the allowlist above and
# nothing else) -- a regression guard: if a field_map entry is ever edited to include one of these,
# this assertion fails loudly at import time, before any file is even read.
_MUST_NEVER_BE_MAPPED = frozenset(_normalise(c) for c in (
    "Aadhar Card No", "Address", "Guest Name", "Guest Gender", "Guest Relation",
    "Payment Details", "Courier Address",
    "Student First Name In Marathi", "Student Middle Name In Marathi",
    "Student Last Name In Marathi", "Student Mother Name In Marathi",
    "Student Father Name In Marathi",
    "Alternate Email Id", "Alternate Mobile No.",
    "CGPA", "Last Exam Grade", "Last Exam Year", "Batch Name",
    "Date of Admission", "Form Submission Date", "Certificate By Courier",
))

UNIVERSITY_CONVOCATION_DETAIL_REPORT = Preset(
    name="University Student Convocation Detail Report (.xls export)",
    expected_columns=frozenset(_normalise(c) for c in HEADER_COLUMNS),
    field_map={
        "prn": ("PRN No.",),
        "name": ("Student First Name", "Student Middle Name", "Student Last Name"),
        "programme": ("Program Name",),
        "school": ("Stream",),
        "email": ("Email Id",),
        "mobile": ("Mobile No.",),
    },
    # This import is the paid-registrations file: every row on it is, by definition, ACTIVE. There
    # is no status column in the file at all (and no sequence_no / seat_no / photo column either --
    # those are simply absent from field_map, which is all "not asked for" ever takes).
    constant_fields={"status": "ACTIVE"},
)

for _field, _sources in UNIVERSITY_CONVOCATION_DETAIL_REPORT.field_map.items():
    for _source in _sources:
        assert _normalise(_source) not in _MUST_NEVER_BE_MAPPED, (
            f"{_source!r} is on the never-map list and cannot be field_map[{_field!r}]")

PRESETS: tuple[Preset, ...] = (UNIVERSITY_CONVOCATION_DETAIL_REPORT,)


# --------------------------------------------------------------------------------------------
# Detection and application
# --------------------------------------------------------------------------------------------
def detect_preset(raw_bytes: bytes, *, engine: str, probe_rows: int = 25) -> tuple[Optional[Preset], Optional[int]]:
    """-> (preset, header_row_index) for the first preset whose full expected header is found among
    the first `probe_rows` rows, scanning top to bottom; (None, None) if nothing matches.

    Nothing above is trusted to be at any particular row: each candidate row's own cells are
    checked, not a hard-coded row number, so a title line, a run date or an extra blank row above
    the real header does not need to be anticipated exactly.
    """
    try:
        probe = pd.read_excel(io.BytesIO(raw_bytes), engine=engine, header=None, dtype=str, nrows=probe_rows)
    except Exception:
        return None, None
    for row_index in range(len(probe)):
        cells = {_normalise(v) for v in probe.iloc[row_index].tolist()}
        cells.discard("")
        for preset in PRESETS:
            if preset.expected_columns <= cells:
                return preset, row_index
    return None, None


def apply_preset(preset: Preset, df: pd.DataFrame) -> tuple[list[str], list[dict]]:
    """Project `df` (already read with the detected header row) down to ONLY what `preset.field_map`
    and `preset.constant_fields` produce. Every other column of `df` is dropped here and never
    turned into a row dict, a sample value, a log line or a database row.

    -> (columns, rows) in exactly the shape `parse_file` returns for the general path: `columns` are
    canonical field names here (there is nothing left to ask an Admin to map), and each row is a
    plain dict keyed by those same canonical names -- which is also one of the two shapes
    `validate_import` already reads a mapping's values from, so nothing downstream needs to know a
    preset was involved at all.
    """
    lookup = {_normalise(c): c for c in df.columns}  # normalised raw header -> the actual column label present

    def _resolve(raw_name: str) -> Optional[str]:
        return lookup.get(_normalise(raw_name))

    keep = [c for c in (_resolve(r) for r in preset.raw_columns_used()) if c is not None]
    safe = df[keep] if keep else df.iloc[:, 0:0]  # only ever the columns field_map actually names

    columns = list(preset.field_map.keys()) + list(preset.constant_fields.keys())
    records: list[dict] = []
    for _, raw_row in safe.iterrows():
        row: dict[str, Any] = {}
        for canonical, sources in preset.field_map.items():
            parts = []
            for source in sources:
                actual = _resolve(source)
                if actual is None:
                    continue
                value = raw_row[actual]
                if value is None:
                    continue
                try:
                    if pd.isna(value):
                        continue
                except (TypeError, ValueError):
                    pass
                text_value = str(value).strip()
                if text_value:
                    parts.append(text_value)
            row[canonical] = " ".join(parts) if parts else None
        for canonical, value in preset.constant_fields.items():
            row[canonical] = value
        records.append(row)
    return columns, records

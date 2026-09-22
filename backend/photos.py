"""backend/photos.py — Phase 3 photo linking by PRN.

Links photos to students by matching filenames (<PRN>.<ext>) against the
students table. Reports unmatched photos and students without photos.
Returns placeholder path for students with no photo.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

_PLACEHOLDER = "static/placeholder.svg"
_SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# PRN from filename: strip extension, strip common suffixes/prefixes
_PRN_RE = re.compile(r"^(.+?)(?:\.[a-zA-Z0-9]+)?$")


@dataclasses.dataclass
class PhotoLinkReport:
    matched_count: int
    unmatched_photos: list[str]    # file paths with no matching student PRN
    unmatched_students: list[dict]  # student dicts {prn, id} with no photo
    # Students whose display data is already frozen: their photo is master data now, so it can only
    # be changed by a logged master patch (TODO Phase 3). The bulk linker reports them and moves on.
    frozen_skipped: list[dict] = dataclasses.field(default_factory=list)


def link_photos_by_prn(
    photo_dir: pathlib.Path | str,
    conn: Connection,
) -> PhotoLinkReport:
    """Scan a directory for photo files, link them to students by PRN.

    A file named ``<PRN>.jpg`` (case-insensitive extensions) is matched to the
    student whose ``prn`` equals the filename stem (case-insensitive).

    Updates ``students.photo_path`` for matched students.
    Returns a report listing:
    - matched_count: how many students were linked
    - unmatched_photos: file names with no student
    - unmatched_students: {prn, id} for students still without a photo
    """
    photo_dir = pathlib.Path(photo_dir)

    # Load all student PRNs from DB, and note which of them are already frozen.
    rows = conn.execute(text(
        "SELECT s.id, s.prn, s.photo_path, EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) AS frozen "
        "FROM students s")).fetchall()
    prn_to_info: dict[str, dict] = {
        r[1].upper(): {"id": r[0], "prn": r[1], "photo_path": r[2], "frozen": r[3]}
        for r in rows
    }

    matched_count = 0
    unmatched_photos: list[str] = []
    matched_prns: set[str] = set()
    frozen_skipped: list[dict] = []

    try:
        for photo_file in sorted(photo_dir.iterdir()):
            if not photo_file.is_file():
                continue
            ext = photo_file.suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue

            prn_upper = photo_file.stem.upper()
            if prn_upper in prn_to_info:
                student_info = prn_to_info[prn_upper]
                photo_path = str(photo_file.resolve())
                already_linked = student_info["photo_path"] == photo_path
                if student_info["frozen"] and not already_linked:
                    # The freeze has happened: this student's photo is only changed by a logged
                    # master patch. Bulk linking must not slip past that (migration 0010 would
                    # refuse the write anyway; reporting it is kinder than an exception).
                    frozen_skipped.append({"prn": student_info["prn"], "id": str(student_info["id"]),
                                           "photo": str(photo_file)})
                    matched_prns.add(prn_upper)
                    continue
                if not already_linked:
                    # Only write when the value would actually change. Writing the same path again
                    # is not harmless: it rewrites the row and moves updated_at, which would break
                    # "re-running the import never modifies an existing student" for anyone who
                    # runs the import a second time with the photo folder attached.
                    conn.execute(
                        text("UPDATE students SET photo_path = :path WHERE id = :sid"),
                        {"path": photo_path, "sid": student_info["id"]},
                    )
                matched_prns.add(prn_upper)
                matched_count += 1
            else:
                unmatched_photos.append(str(photo_file))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    unmatched_students = [
        {"prn": info["prn"], "id": str(info["id"])}
        for prn_upper, info in prn_to_info.items()
        if prn_upper not in matched_prns
    ]

    return PhotoLinkReport(
        matched_count=matched_count,
        unmatched_photos=unmatched_photos,
        unmatched_students=unmatched_students,
        frozen_skipped=frozen_skipped,
    )


def resolve_student_photo(photo_path: str | None) -> str:
    """Return the given photo path if it exists, otherwise return the placeholder."""
    if photo_path is not None:
        p = pathlib.Path(photo_path)
        if p.exists():
            return str(photo_path)
    return _PLACEHOLDER

"""backend/photos.py — Photo linking and management by PRN.

Supports:
1. Legacy Phase 3 <PRN>.<ext> photo linking.
2. Structured university photo export parsing and linking:
   <seq>_PROFILE_IMAGE_PRN_No_<PRN>_Name_<Name>.<ext>
3. Master-assisted matching (Option B) using the student master spreadsheet (.xlsx / .xls)
   to resolve university Enrollment No / Roll No to student PRN.
4. Comprehensive, non-silent error and mismatch accounting:
   - Clean one-to-one matches
   - Students with no matching photo
   - Orphaned photos with no matching student
   - Duplicate PRNs among photo filenames
   - Duplicate PRNs among student rows
   - Malformed filenames not matching expected patterns
   - Frozen display data safety skipping
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import io
import os
import pathlib
import re
import threading
import zipfile
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from backend.importer import excel_engine_for
from backend.photo_storage import LocalPhotoStore, PhotoStore, PhotoStoreError

_PLACEHOLDER = "static/placeholder.svg"
_SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# PRN from simple filename (<PRN>.<ext>)
_SIMPLE_PRN_RE = re.compile(r"^(.+?)(?:\.[a-zA-Z0-9]+)?$")

# Structured university export pattern:
# e.g. 1_PROFILE_IMAGE_PRN_No_BSFS220037_Name_Akashkumar Gulab Shirsath.jpg
_STRUCTURED_PHOTO_RE = re.compile(
    r"^(?:(?P<seq>\d+)_)?PROFILE_IMAGE_PRN[ _-]No[ _-](?P<prn>.*?)_Name[ _-](?P<name>.*?)\.(?P<ext>[a-zA-Z0-9]+)$",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class ParsedPhotoFilename:
    prn: str
    name: str
    ext: str
    seq: Optional[str]
    original_filename: str
    clean_filename: str = ""


@dataclasses.dataclass
class PhotoLinkReport:
    matched_count: int
    unmatched_photos: list[str]      # file paths with no matching student PRN
    unmatched_students: list[dict]   # student dicts {prn, id} with no photo
    frozen_skipped: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PhotoImportReport:
    matched_count: int
    matched_students: list[dict]
    unmatched_students: list[dict]
    orphaned_photos: list[dict]
    duplicate_photo_prns: list[dict]
    duplicate_student_prns: list[dict]
    malformed_filenames: list[str]
    frozen_skipped: list[dict] = dataclasses.field(default_factory=list)
    # Matched photos whose bytes could not be written to the photo store (or read out of the ZIP):
    # [{"filename", "prn", "reason"}]. The student's photo_path is left exactly as it was.
    storage_failed: list[dict] = dataclasses.field(default_factory=list)


def parse_photo_filename(filename: str) -> Optional[ParsedPhotoFilename]:
    """Extract PRN, Name, and Extension from a structured profile photo filename.

    Tolerant of:
    - Leading sequence prefix (e.g. '1_', '18_', '1432_') or absence thereof.
    - Case variations in prefix, separators, PRN, and extension.
    - Hyphens, spaces, and alphanumeric mixes in the PRN token.
    - Mixed supported extensions (.jpg, .jpeg, .png, etc.).

    Returns ParsedPhotoFilename if valid, or None if the filename is malformed.
    """
    clean_name = os.path.basename(filename).strip()
    m = _STRUCTURED_PHOTO_RE.match(clean_name)
    if not m:
        return None

    ext = m.group("ext").strip()
    ext_lower = f".{ext.lower()}"
    if ext_lower not in _SUPPORTED_EXTENSIONS:
        return None

    prn = m.group("prn").strip().upper()
    if not prn:
        return None

    name = m.group("name").strip()
    seq = m.group("seq")
    safe_filename = "".join(c for c in clean_name if ord(c) >= 32 and c not in '<>:"/\\|?*').strip()

    return ParsedPhotoFilename(
        prn=prn,
        name=name,
        ext=ext,
        seq=seq,
        original_filename=clean_name,
        clean_filename=safe_filename,
    )


def extract_student_id_mappings(
    excel_source: Union[bytes, io.IOBase, pathlib.Path, str],
    filename: str = "student_master.xlsx",
) -> tuple[dict[str, str], list[dict]]:
    """Extract a mapping from Enrollment No/Roll No and PRN No to canonical PRN from the master spreadsheet.

    Returns:
        (id_to_canonical_prn, duplicate_student_prns)
    """
    engine = excel_engine_for(filename) or "openpyxl"

    raw_bytes: bytes
    if isinstance(excel_source, (pathlib.Path, str)):
        raw_bytes = pathlib.Path(excel_source).read_bytes()
    elif isinstance(excel_source, io.IOBase):
        raw_bytes = excel_source.read()
    else:
        raw_bytes = excel_source

    # Probe rows to find header containing 'PRN'
    probe = pd.read_excel(io.BytesIO(raw_bytes), engine=engine, header=None, dtype=str, nrows=25)
    header_row_idx = 0
    for idx, row in probe.iterrows():
        row_cells = [str(v).strip().lower() for v in row.dropna()]
        if any("prn" in c for c in row_cells):
            header_row_idx = idx
            break

    df = pd.read_excel(io.BytesIO(raw_bytes), engine=engine, header=header_row_idx, dtype=str, keep_default_na=False)

    # Locate PRN column and Enrollment column
    prn_col = None
    enroll_col = None
    for col in df.columns:
        norm = " ".join(str(col).strip().lower().split())
        if norm in ("prn no.", "prn no", "prn", "prn number"):
            prn_col = col
        elif norm in ("enrollment no/roll no", "enrollment no", "roll no", "enrollment number"):
            enroll_col = col

    mapping: dict[str, str] = {}
    prn_seen_rows: dict[str, list[int]] = defaultdict(list)

    for idx, row in df.iterrows():
        row_num = idx + header_row_idx + 2  # 1-indexed spreadsheet line number
        raw_prn = str(row[prn_col]).strip() if prn_col and row.get(prn_col) else ""
        raw_enroll = str(row[enroll_col]).strip() if enroll_col and row.get(enroll_col) else ""

        if not raw_prn and not raw_enroll:
            continue

        canonical_prn = (raw_prn or raw_enroll).strip().upper()
        if raw_prn:
            prn_seen_rows[canonical_prn].append(row_num)

        mapping[canonical_prn] = canonical_prn
        if raw_enroll:
            mapping[raw_enroll.strip().upper()] = canonical_prn

    duplicate_student_prns = [
        {"prn": p, "rows": rows}
        for p, rows in prn_seen_rows.items()
        if len(rows) > 1
    ]

    return mapping, duplicate_student_prns


def inspect_photo_zip(zip_path: Union[pathlib.Path, str]) -> dict:
    """Read only the ZIP's table of contents (no photo is decompressed): how many entries look like
    university photo files and which names are malformed. Raises ValueError if it is not a readable ZIP."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = [os.path.basename(n) for n in zf.namelist() if not n.endswith("/")]
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ValueError(f"not a readable ZIP file ({type(exc).__name__})") from None
    names = [n for n in names if n and not n.startswith(".")]
    malformed = [n for n in names if parse_photo_filename(n) is None]
    return {"entries": len(names), "photos": len(names) - len(malformed), "malformed": len(malformed)}


def import_photos_from_zip(
    zip_source: Union[bytes, io.IOBase, pathlib.Path, str],
    engine_or_conn: Union[Engine, Connection],
    excel_source: Optional[Union[bytes, io.IOBase, pathlib.Path, str]] = None,
    excel_filename: str = "Untitled spreadsheet.xlsx",
    dest_dir: Union[pathlib.Path, str] = "photos",
    prn_filter: Optional[Iterable[str]] = None,
    store: Optional[PhotoStore] = None,
    max_workers: Optional[int] = None,
) -> PhotoImportReport:
    """Import and match photos from a ZIP archive to existing student records.

    Option B: If `excel_source` is provided, maps Enrollment No / Roll No to PRN No.
    Writes each matched photo through `store` (backend/photo_storage.py: a local folder or
    Cloudinary) and updates `students.photo_path`. Without `store`, photos go to the folder
    `dest_dir` with `<dest_dir>/<name>` keys, exactly as before the store existed.
    If `prn_filter` is specified, scopes the operation to only those PRNs.

    The ZIP is read one entry at a time (a path is never loaded whole), and a photo that cannot be
    stored is reported in `storage_failed` rather than aborting the run or being counted as linked.
    """
    if store is None:
        dest_path = pathlib.Path(dest_dir)
        dest_path.mkdir(parents=True, exist_ok=True)
        store = LocalPhotoStore(dest_path, key_prefix=dest_path.as_posix())

    filter_prns: Optional[set[str]] = (
        {p.strip().upper() for p in prn_filter} if prn_filter is not None else None
    )

    # 1. Parse Excel mapping if provided
    id_mapping: dict[str, str] = {}
    excel_dup_students: list[dict] = []
    if excel_source is not None:
        id_mapping, excel_dup_students = extract_student_id_mappings(excel_source, filename=excel_filename)

    # 2. Open ZIP archive
    if isinstance(zip_source, (pathlib.Path, str)):
        zf = zipfile.ZipFile(zip_source, "r")
    elif isinstance(zip_source, io.IOBase):
        zf = zipfile.ZipFile(zip_source, "r")
    else:
        zf = zipfile.ZipFile(io.BytesIO(zip_source), "r")

    # 3. Read and categorize zip entries
    valid_photos: list[tuple[ParsedPhotoFilename, str]] = []
    malformed_filenames: list[str] = []
    photos_by_prn: dict[str, list[str]] = defaultdict(list)

    with zf:
        for zip_entry in zf.namelist():
            if zip_entry.endswith("/"):
                continue
            base_name = os.path.basename(zip_entry)
            if not base_name or base_name.startswith("."):
                continue

            parsed = parse_photo_filename(base_name)
            if parsed is None:
                malformed_filenames.append(base_name)
                continue

            valid_photos.append((parsed, zip_entry))
            photos_by_prn[parsed.prn].append(base_name)

    duplicate_photo_prns = [
        {"prn": prn, "filenames": files}
        for prn, files in photos_by_prn.items()
        if len(files) > 1
    ]

    # 4. Connect to database and load students
    close_conn = False
    if isinstance(engine_or_conn, Engine):
        conn = engine_or_conn.connect()
        close_conn = True
    else:
        conn = engine_or_conn

    try:
        # Load students
        db_rows = conn.execute(text(
            "SELECT s.id, s.prn, s.name, s.photo_path, "
            "EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = s.id) AS frozen "
            "FROM students s"
        )).fetchall()

        if filter_prns is not None:
            db_rows = [r for r in db_rows if r[1].strip().upper() in filter_prns]

        # Check for DB duplicate PRNs
        db_students_by_prn: dict[str, dict] = {}
        db_dup_counts: dict[str, list] = defaultdict(list)
        for r in db_rows:
            norm_prn = r[1].strip().upper()
            db_dup_counts[norm_prn].append(r[0])
            if norm_prn not in db_students_by_prn:
                db_students_by_prn[norm_prn] = {
                    "id": r[0],
                    "prn": r[1],
                    "name": r[2],
                    "photo_path": r[3],
                    "frozen": r[4],
                }

        duplicate_student_prns = list(excel_dup_students)
        for p, sids in db_dup_counts.items():
            if len(sids) > 1 and not any(d["prn"] == p for d in duplicate_student_prns):
                duplicate_student_prns.append({"prn": p, "student_ids": [str(s) for s in sids]})

        # 5. Perform Matching
        matched_students: list[dict] = []
        orphaned_photos: list[dict] = []
        frozen_skipped: list[dict] = []
        storage_failed: list[dict] = []
        matched_student_ids: set[Any] = set()
        seen_matched_prns: set[str] = set()

        # Re-open zip to extract matched photos
        if isinstance(zip_source, (pathlib.Path, str)):
            read_zf = zipfile.ZipFile(zip_source, "r")
        elif isinstance(zip_source, io.IOBase):
            zip_source.seek(0)
            read_zf = zipfile.ZipFile(zip_source, "r")
        else:
            read_zf = zipfile.ZipFile(io.BytesIO(zip_source), "r")

        to_upload: list[tuple[int, Any, str, dict, str, str]] = []
        with read_zf:
            for idx, (parsed, zip_entry) in enumerate(valid_photos):
                photo_key = parsed.prn
                # Resolve canonical PRN: direct or via spreadsheet mapping
                canonical_prn = id_mapping.get(photo_key, photo_key)

                if filter_prns is not None and canonical_prn not in filter_prns and photo_key not in filter_prns:
                    continue

                student = db_students_by_prn.get(canonical_prn)

                if student is None:
                    orphaned_photos.append({
                        "filename": parsed.original_filename,
                        "extracted_prn": parsed.prn,
                    })
                    continue

                student_id = student["id"]
                if student_id in matched_student_ids:
                    # Duplicate photo for the same student already matched earlier
                    continue

                matched_student_ids.add(student_id)
                seen_matched_prns.add(canonical_prn)

                # The key is a bare POSIX path ("photos/filename.jpg") whatever the store, so it
                # works on any OS and inside the Linux container without backslashes.
                stored_name = parsed.clean_filename or parsed.original_filename
                photo_path_str = store.key_for(stored_name)

                # Check frozen safety
                if student["frozen"] and student["photo_path"] != photo_path_str:
                    frozen_skipped.append({
                        "prn": student["prn"],
                        "id": str(student["id"]),
                        "photo": photo_path_str,
                    })
                    continue

                to_upload.append((idx, parsed, zip_entry, student, stored_name, photo_path_str))

            # Bounded concurrent photo saving (Phase 3 optimization)
            workers = max_workers
            if workers is None:
                try:
                    from backend.config import get_settings
                    workers = getattr(get_settings(), "photo_import_concurrency", 4)
                except Exception:
                    workers = 4
            workers = max(1, int(workers))

            results: list[tuple[int, Any, dict, str, str, bool, Optional[str]]] = []
            zip_lock = threading.Lock()

            def _process_candidate(item):
                i_idx, p_parsed, z_entry, s_student, s_name, p_str = item
                try:
                    with zip_lock:
                        data = read_zf.read(z_entry)
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
                    return (i_idx, p_parsed, s_student, s_name, p_str, False,
                            "This file inside the ZIP is damaged and could not be read.")
                try:
                    store.save(s_name, data)
                    return (i_idx, p_parsed, s_student, s_name, p_str, True, None)
                except PhotoStoreError as exc:
                    return (i_idx, p_parsed, s_student, s_name, p_str, False, str(exc))
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
                    return (i_idx, p_parsed, s_student, s_name, p_str, False,
                            "This file inside the ZIP is damaged and could not be read.")
                except Exception as exc:
                    return (i_idx, p_parsed, s_student, s_name, p_str, False, str(exc))

            if workers <= 1 or len(to_upload) <= 1:
                for item in to_upload:
                    results.append(_process_candidate(item))
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    results = list(executor.map(_process_candidate, to_upload))

            # Sort by original encounter order so result arrays remain fully deterministic
            results.sort(key=lambda r: r[0])

            for _, parsed, student, stored_name, photo_path_str, success, reason in results:
                if not success:
                    storage_failed.append({"filename": parsed.original_filename, "prn": student["prn"], "reason": reason})
                    continue

                if student["photo_path"] != photo_path_str:
                    conn.execute(
                        text("UPDATE students SET photo_path = :path WHERE id = :sid"),
                        {"path": photo_path_str, "sid": student["id"]},
                    )

                matched_students.append({
                    "student_id": str(student["id"]),
                    "prn": student["prn"],
                    "photo_path": photo_path_str,
                })

        conn.commit()

        unmatched_students = [
            {"student_id": str(s["id"]), "prn": s["prn"], "name": s["name"]}
            for norm_prn, s in db_students_by_prn.items()
            if s["id"] not in matched_student_ids
        ]

        return PhotoImportReport(
            matched_count=len(matched_students),
            matched_students=matched_students,
            unmatched_students=unmatched_students,
            orphaned_photos=orphaned_photos,
            duplicate_photo_prns=duplicate_photo_prns,
            duplicate_student_prns=duplicate_student_prns,
            malformed_filenames=malformed_filenames,
            frozen_skipped=frozen_skipped,
            storage_failed=storage_failed,
        )

    except Exception:
        conn.rollback()
        raise
    finally:
        if close_conn:
            conn.close()


def link_photos_by_prn(
    photo_dir: pathlib.Path | str,
    conn: Connection,
) -> PhotoLinkReport:
    """Legacy helper: scan a directory for photo files, link them to students by PRN.

    Maintained for backwards-compatibility with Phase 3 tests.
    """
    photo_dir = pathlib.Path(photo_dir)

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
                photo_path = photo_dir.as_posix().rstrip("/") + "/" + photo_file.name
                already_linked = student_info["photo_path"] == photo_path
                if student_info["frozen"] and not already_linked:
                    frozen_skipped.append({"prn": student_info["prn"], "id": str(student_info["id"]),
                                           "photo": str(photo_file)})
                    matched_prns.add(prn_upper)
                    continue
                if not already_linked:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m backend.photos <zip> [spreadsheet]`: the same import the Admin screen runs, into the
    same configured photo store (PHOTO_STORAGE), unless --dest names a local folder explicitly."""
    import argparse
    import sys

    from backend import photo_storage
    from backend.config import get_settings

    parser = argparse.ArgumentParser(description="Import student profile photos by PRN from zip archive.")
    parser.add_argument("zip_path", nargs="?", default="Student Profile Image.zip", help="Path to zip file containing student photos.")
    parser.add_argument("excel_path", nargs="?", default="Untitled spreadsheet.xlsx", help="Path to master spreadsheet (.xlsx / .xls).")
    parser.add_argument("--dest", default=None,
                        help="Write photos to this local folder instead of the configured photo store (PHOTO_STORAGE).")
    parser.add_argument("--db-url", default=None, help="PostgreSQL connection URL.")

    args = parser.parse_args(argv)

    zip_file = pathlib.Path(args.zip_path)
    if not zip_file.exists():
        print(f"Error: ZIP file not found at {zip_file}", file=sys.stderr)
        return 1
    if not zipfile.is_zipfile(zip_file):
        print(f"Error: {zip_file} is not a readable ZIP file. Nothing was imported.", file=sys.stderr)
        return 1

    excel_file = pathlib.Path(args.excel_path) if args.excel_path else None
    if excel_file and not excel_file.exists():
        excel_file = None

    if args.dest:
        store: PhotoStore = LocalPhotoStore(args.dest, key_prefix=pathlib.PurePath(args.dest).as_posix())
    else:
        try:
            store = photo_storage.build_store(get_settings())
        except photo_storage.PhotoStorageConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    if store.write_refusal:
        print(f"Error: {store.write_refusal}", file=sys.stderr)
        return 2

    db_url = args.db_url or os.getenv("DATABASE_URL", "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db")
    if "@db:5432" in db_url:
        db_url = db_url.replace("@db:5432", "@localhost:5432")

    engine = create_engine(db_url)

    print(f"Importing photos from {zip_file}...")
    print(f"Photo storage: {store.describe()}")
    if excel_file:
        print(f"Using spreadsheet mapping from {excel_file}...")

    report = import_photos_from_zip(
        zip_source=zip_file,
        engine_or_conn=engine,
        excel_source=excel_file,
        excel_filename=excel_file.name if excel_file else "Untitled spreadsheet.xlsx",
        store=store,
    )

    print("\n--- PHOTO IMPORT SUMMARY ---")
    print(f"Clean Matches Linked:     {report.matched_count}")
    print(f"Unmatched Students in DB: {len(report.unmatched_students)}")
    print(f"Orphaned Photos in Zip:   {len(report.orphaned_photos)}")
    print(f"Duplicate Photo PRNs:     {len(report.duplicate_photo_prns)}")
    print(f"Duplicate Student PRNs:   {len(report.duplicate_student_prns)}")
    print(f"Malformed Filenames:      {len(report.malformed_filenames)}")
    print(f"Frozen Students Skipped:  {len(report.frozen_skipped)}")
    print(f"Storage Write Failures:   {len(report.storage_failed)}")
    for failure in report.storage_failed:
        print(f"  - {failure['prn']}: {failure['filename']} -- {failure['reason']}")
    return 1 if report.storage_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

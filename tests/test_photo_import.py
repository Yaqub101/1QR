"""Tests for student photo import by PRN.

Verifies:
1. Filename parser extracting PRN, tolerant of mixed case, spaces, hyphens, and mixed extensions.
2. Malformed filename detection (filenames not matching expected pattern).
3. Clean one-to-one matches (photo links to student and updates photo_path).
4. Students with no matching photo (explicitly reported and counted).
5. Photos with no matching student (orphaned photos explicitly reported and counted).
6. Duplicate PRNs among photo filenames (explicitly reported and counted).
7. Duplicate PRNs among student rows (explicitly detected and reported).
8. Master-assisted matching (Option B: resolving Enrollment No/Roll No in photo to PRN No. via spreadsheet).
9. Existing QR tokens untouched and never regenerated.
10. Existing activity events and student journey state untouched.
11. Frozen display snapshot guard respected (bulk import skips frozen students).
"""
import io
import pathlib
import uuid
import zipfile
import pytest
from sqlalchemy import text

from backend.photos import (
    parse_photo_filename,
    import_photos_from_zip,
    PhotoImportReport,
    ParsedPhotoFilename,
)
from backend import qr_tokens
from backend.snapshot import freeze_display_data
from tests.test_station_engine import engine  # noqa: F401 (module-scoped migrated DB)


# --------------------------------------------------------------------------- #
# 1. Filename Parser Tests
# --------------------------------------------------------------------------- #

def test_filename_parser_valid_variants():
    """Tolerates sequence prefixes, case variations, spaces, hyphens, and mixed extensions."""
    samples = [
        ("1_PROFILE_IMAGE_PRN_No_BSFS220037_Name_Akashkumar Gulab Shirsath.jpg", "BSFS220037", "Akashkumar Gulab Shirsath", "jpg"),
        ("2_PROFILE_IMAGE_PRN_No_202402106052_Name_Vaishnavi Ravindra Jagdale.png", "202402106052", "Vaishnavi Ravindra Jagdale", "png"),
        ("3_PROFILE_IMAGE_PRN_No_202401109014_Name_Pratiksha Madhav Deshmukh.JPEG", "202401109014", "Pratiksha Madhav Deshmukh", "JPEG"),
        ("18_PROFILE_IMAGE_PRN_No_BBA-MKTG-112_Name_Krushna Narayan Ghuge.JPEG", "BBA-MKTG-112", "Krushna Narayan Ghuge", "JPEG"),
        ("15_PROFILE_IMAGE_PRN_No_23DipFD5_Name_Rida Maryam Dilawar Khan.png", "23DIPFD5", "Rida Maryam Dilawar Khan", "png"),
        ("561_PROFILE_IMAGE_PRN_No_HOCSTY 34_Name_Sameer Sanjeev Pawar.JPEG", "HOCSTY 34", "Sameer Sanjeev Pawar", "JPEG"),
        ("1438_PROFILE_IMAGE_PRN_No_PhDEDU2206_Name_Kailash Parashram Ghaywat.png", "PHDEDU2206", "Kailash Parashram Ghaywat", "png"),
        # Omission of sequence prefix
        ("PROFILE_IMAGE_PRN_No_ABC123_Name_Test Student.png", "ABC123", "Test Student", "png"),
        # Lowercase prefixes and separators
        ("10_profile_image_prn_no_xyz999_name_lower student.jpeg", "XYZ999", "lower student", "jpeg"),
    ]
    for filename, expected_prn, expected_name, expected_ext in samples:
        parsed = parse_photo_filename(filename)
        assert parsed is not None, f"Failed to parse valid filename: {filename}"
        assert parsed.prn == expected_prn.upper()
        assert parsed.name == expected_name
        assert parsed.ext.lower() == expected_ext.lower()


def test_filename_parser_malformed():
    """Filenames that do not follow the expected pattern return None."""
    malformed = [
        "random_image.jpg",
        "1_PROFILE_IMAGE_no_prn.png",
        "student_photo.png",
        "PROFILE_IMAGE_Name_Only_no_prn.jpg",
        "notes.txt",
        "README.md",
    ]
    for filename in malformed:
        parsed = parse_photo_filename(filename)
        assert parsed is None, f"Expected None for malformed filename: {filename}"


# --------------------------------------------------------------------------- #
# Helpers for DB & Mock Data
# --------------------------------------------------------------------------- #

def _create_test_zip(files: dict[str, bytes]) -> bytes:
    """Build a zip archive in-memory from {filename: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _insert_student(conn, prn: str, name: str, programme: str = "B.Tech CS", school: str = "Eng", photo_path: str = None) -> str:
    res = conn.execute(
        text("INSERT INTO students (prn, name, programme, school, status, photo_path) "
             "VALUES (:prn, :name, :prog, :school, 'ACTIVE', :photo) RETURNING id"),
        {"prn": prn, "name": name, "prog": programme, "school": school, "photo": photo_path}
    )
    return str(res.scalar_one())


# --------------------------------------------------------------------------- #
# 2. Integration / Core Import Routine Tests
# --------------------------------------------------------------------------- #

def test_clean_one_to_one_matches(engine, tmp_path):
    """Clean matches link photo, update students.photo_path, and write file to dest_dir."""
    p1 = f"PRN101_{uuid.uuid4().hex[:6]}"
    p2 = f"PRN102_{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        s1_id = _insert_student(conn, p1, "Alice Smith")
        s2_id = _insert_student(conn, p2, "Bob Jones")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p1}_Name_Alice Smith.png": b"dummy_png_data_1",
        f"2_PROFILE_IMAGE_PRN_No_{p2}_Name_Bob Jones.JPEG": b"dummy_jpeg_data_2",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p1, p2])

    matched_dict = {m["prn"]: m for m in report.matched_students}
    assert p1 in matched_dict
    assert p2 in matched_dict
    assert report.matched_count == 2
    assert len(report.unmatched_students) == 0
    assert len(report.orphaned_photos) == 0

    # Verify DB update
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT prn, photo_path FROM students WHERE prn IN (:p1, :p2)"),
            {"p1": p1, "p2": p2}
        ).fetchall()
        assert len(rows) == 2
        for prn, path_str in rows:
            assert path_str is not None
            p = pathlib.Path(path_str)
            assert p.exists()
            assert prn in p.name


def test_students_with_no_matching_photo(engine, tmp_path):
    """Students in DB who lack a photo in the zip are explicitly reported in unmatched_students."""
    p_with_photo = f"PRN_HAS_{uuid.uuid4().hex[:6]}"
    p_missing = f"PRN_MISS_{uuid.uuid4().hex[:6]}"

    with engine.begin() as conn:
        _insert_student(conn, p_with_photo, "Has Photo")
        s_missing_id = _insert_student(conn, p_missing, "Missing Photo")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_with_photo}_Name_Has Photo.png": b"image_data",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(
        zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_with_photo, p_missing]
    )

    assert report.matched_count == 1
    assert len(report.unmatched_students) == 1
    unmatched = report.unmatched_students[0]
    assert unmatched["prn"] == p_missing
    assert unmatched["student_id"] == s_missing_id

    # Verify DB photo_path remains None
    with engine.connect() as conn:
        photo_path = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"),
            {"p": p_missing}
        ).scalar()
        assert photo_path is None


def test_photos_with_no_matching_student_orphaned(engine, tmp_path):
    """Photos in the zip that do not correspond to any student are explicitly reported as orphaned."""
    p_existing = f"PRN_EXIST_{uuid.uuid4().hex[:6]}"
    p_orphan1 = f"PRN_ORPH1_{uuid.uuid4().hex[:6]}"
    p_orphan2 = f"PRN_ORPH2_{uuid.uuid4().hex[:6]}"

    with engine.begin() as conn:
        _insert_student(conn, p_existing, "Existing Student")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_existing}_Name_Existing Student.png": b"data_1",
        f"2_PROFILE_IMAGE_PRN_No_{p_orphan1}_Name_Orphan Student One.png": b"data_orphan_1",
        f"3_PROFILE_IMAGE_PRN_No_{p_orphan2}_Name_Orphan Student Two.JPEG": b"data_orphan_2",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(
        zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_existing, p_orphan1, p_orphan2]
    )

    assert report.matched_count == 1
    assert len(report.orphaned_photos) == 2
    orphan_prns = {o["extracted_prn"] for o in report.orphaned_photos}
    assert orphan_prns == {p_orphan1.upper(), p_orphan2.upper()}


def test_duplicate_prns_among_photo_filenames(engine, tmp_path):
    """When the zip contains multiple files with the same normalized PRN, they are flagged."""
    p_dup = f"BCATY15_{uuid.uuid4().hex[:6]}"

    with engine.begin() as conn:
        _insert_student(conn, p_dup, "Vishal Shaha")

    zip_bytes = _create_test_zip({
        f"670_PROFILE_IMAGE_PRN_No_{p_dup}_Name_Vishal Sham Shaha.png": b"data_v1",
        f"671_PROFILE_IMAGE_PRN_No_{p_dup.lower()}_Name_Vishal Sham Shaha.png": b"data_v2",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_dup])

    assert len(report.duplicate_photo_prns) == 1
    dup_entry = report.duplicate_photo_prns[0]
    assert dup_entry["prn"] == p_dup.upper()
    assert len(dup_entry["filenames"]) == 2
    # The student is still linked (first occurrence used)
    assert report.matched_count == 1


def test_malformed_filenames_handling(engine, tmp_path):
    """Files in the zip that fail to match the regex pattern are recorded in malformed_filenames."""
    p_valid = f"PRN_VAL_{uuid.uuid4().hex[:6]}"

    with engine.begin() as conn:
        _insert_student(conn, p_valid, "Valid Student")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_valid}_Name_Valid Student.png": b"valid_data",
        "random_snapshot.jpg": b"bad_file_1",
        "corrupted_filename.png": b"bad_file_2",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_valid])

    assert report.matched_count == 1
    assert len(report.malformed_filenames) == 2
    assert "random_snapshot.jpg" in report.malformed_filenames
    assert "corrupted_filename.png" in report.malformed_filenames


def test_duplicate_prns_among_student_rows(engine, tmp_path):
    """Detects and reports duplicate PRNs among student records in the spreadsheet."""
    p_dup = f"DUP_{uuid.uuid4().hex[:6].upper()}"
    enroll1 = f"EN1_{uuid.uuid4().hex[:6].upper()}"
    enroll2 = f"EN2_{uuid.uuid4().hex[:6].upper()}"

    excel_content = io.BytesIO()
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    # Row 1-4 blank/title
    for _ in range(4):
        ws.append(["Title"])
    # Row 5 header
    headers = [
        "", "Sr.No", "Signature", "Program Name", "Stream",
        "Student First Name", "Student Middle Name", "Student Last Name",
        "Student Mother Name", "Student Father Name",
        "Student First Name In Marathi", "Student Middle Name In Marathi",
        "Student Last Name In Marathi", "Student Mother Name In Marathi",
        "Student Father Name In Marathi",
        "Gender", "PRN No.", "Enrollment No/Roll No"
    ]
    ws.append(headers)
    # Row 6: Student 1
    ws.append(["", "1", None, "B.Tech", "Eng", "Alice", "", "Smith", "", "", "", "", "", "", "", "F", p_dup, enroll1])
    # Row 7: Duplicate PRN
    ws.append(["", "2", None, "B.Tech", "Eng", "Duplicate", "", "Person", "", "", "", "", "", "", "", "M", p_dup, enroll2])
    wb.save(excel_content)

    with engine.begin() as conn:
        _insert_student(conn, p_dup, "Alice Smith")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{enroll1}_Name_Alice Smith.png": b"data",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(
        zip_bytes,
        engine,
        excel_source=excel_content.getvalue(),
        dest_dir=dest_dir,
        prn_filter=[p_dup, enroll1, enroll2],
    )

    assert len(report.duplicate_student_prns) >= 1
    dup_prns = [d["prn"] for d in report.duplicate_student_prns]
    assert p_dup in dup_prns


def test_master_assisted_matching_option_b(engine, tmp_path):
    """Option B: The routine uses the .xlsx student master file to map Enrollment No/Roll No to PRN No."""
    uid = uuid.uuid4().hex[:6]
    prn1 = f"202250128037_{uid}"
    enroll1 = f"BSFS220037_{uid}"
    prn2 = f"202001101015_{uid}"
    enroll2 = prn2

    excel_content = io.BytesIO()
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(4):
        ws.append(["Header padding"])
    headers = [
        "", "Sr.No", "Signature", "Program Name", "Stream",
        "Student First Name", "Student Middle Name", "Student Last Name",
        "Student Mother Name", "Student Father Name",
        "Student First Name In Marathi", "Student Middle Name In Marathi",
        "Student Last Name In Marathi", "Student Mother Name In Marathi",
        "Student Father Name In Marathi",
        "Gender", "PRN No.", "Enrollment No/Roll No"
    ]
    ws.append(headers)
    # Akashkumar Gulab Shirsath: PRN=prn1, Enroll=enroll1
    ws.append(["", "1", None, "B.Sc", "Sci", "Akashkumar", "Gulab", "Shirsath", "", "", "", "", "", "", "", "M", prn1, enroll1])
    # Student 2 where PRN == Enroll
    ws.append(["", "2", None, "B.Tech", "Eng", "Ujwal", "Devidas", "Kuche", "", "", "", "", "", "", "", "M", prn2, enroll2])
    wb.save(excel_content)

    with engine.begin() as conn:
        s1_id = _insert_student(conn, prn1, "Akashkumar Gulab Shirsath")
        s2_id = _insert_student(conn, prn2, "Ujwal Devidas Kuche")

    # Photo 1 is named with enroll1; Photo 2 is named with prn2
    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{enroll1}_Name_Akashkumar Gulab Shirsath.jpg": b"akash_photo",
        f"2_PROFILE_IMAGE_PRN_No_{prn2}_Name_Ujwal Devidas Kuche.png": b"ujwal_photo",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(
        zip_bytes,
        engine,
        excel_source=excel_content.getvalue(),
        dest_dir=dest_dir,
        prn_filter=[prn1, prn2, enroll1, enroll2],
    )

    assert report.matched_count == 2
    assert len(report.unmatched_students) == 0
    assert len(report.orphaned_photos) == 0

    with engine.connect() as conn:
        akash_photo = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"),
            {"p": prn1}
        ).scalar()
        ujwal_photo = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"),
            {"p": prn2}
        ).scalar()
        assert akash_photo is not None and enroll1 in akash_photo
        assert ujwal_photo is not None and prn2 in ujwal_photo


def test_existing_qr_tokens_untouched(engine, tmp_path):
    """Existing QR tokens must NEVER be touched, updated, or regenerated during photo import."""
    p_token = f"PRN_TOK_{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        s_id = _insert_student(conn, p_token, "Token Guard Student")

    # Generate an active QR token for this student
    with engine.connect() as conn:
        token_info = qr_tokens.generate_missing_tokens(engine)

    with engine.connect() as conn:
        before_token = conn.execute(
            text("SELECT id, token, active, generated_at FROM qr_tokens WHERE student_id = :sid"),
            {"sid": s_id},
        ).mappings().one()

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_token}_Name_Token Guard Student.png": b"photo_data",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_token])
    assert report.matched_count == 1

    with engine.connect() as conn:
        after_token = conn.execute(
            text("SELECT id, token, active, generated_at FROM qr_tokens WHERE student_id = :sid"),
            {"sid": s_id},
        ).mappings().one()

    # Byte-identical token state
    assert dict(before_token) == dict(after_token)


def test_activity_events_untouched(engine, tmp_path):
    """Photo import must not affect activity events or station journey state."""
    p_journey = f"PRN_JRN_{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        s_id = _insert_student(conn, p_journey, "Journey Student")
        # Add a Registration event
        conn.execute(
            text("INSERT INTO activity_events (student_id, activity, kind, operator_id, flags, details) "
                 "VALUES (:sid, 'REGISTRATION', 'COMPLETE', gen_random_uuid(), ARRAY[]::text[], '{}'::jsonb)"),
            {"sid": s_id},
        )

    with engine.connect() as conn:
        before_events = conn.execute(
            text("SELECT event_id, activity, kind FROM activity_events WHERE student_id = :sid"),
            {"sid": s_id},
        ).fetchall()

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_journey}_Name_Journey Student.png": b"photo_data",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_journey])
    assert report.matched_count == 1

    with engine.connect() as conn:
        after_events = conn.execute(
            text("SELECT event_id, activity, kind FROM activity_events WHERE student_id = :sid"),
            {"sid": s_id},
        ).fetchall()

    assert before_events == after_events


def test_frozen_display_snapshot_guard_respected(engine, tmp_path):
    """Bulk import skips frozen students whose photos differ, preventing master data trigger failures."""
    p_frozen = f"PRN_FRZ_{uuid.uuid4().hex[:6]}"
    initial_photo = "photos/original_frozen.png"

    with engine.begin() as conn:
        s_id = _insert_student(conn, p_frozen, "Frozen Student", photo_path=initial_photo)
        # Freeze display data
        freeze_display_data(conn)

    # Attempt to import a different photo for this frozen student
    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{p_frozen}_Name_Frozen Student.png": b"new_photo_data",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_frozen])

    assert len(report.frozen_skipped) == 1
    assert report.frozen_skipped[0]["prn"] == p_frozen
    assert report.matched_count == 0

    # Ensure DB was not changed
    with engine.connect() as conn:
        current_path = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"),
            {"p": p_frozen}
        ).scalar()
        assert current_path == initial_photo


def test_filename_with_control_characters_is_sanitized_and_saved(engine, tmp_path):
    """Filenames with control characters (such as tabs in university exports) are safely sanitized and saved."""
    p_tab = f"23BAIJE24_{uuid.uuid4().hex[:6].upper()}"
    with engine.begin() as conn:
        s_id = _insert_student(conn, p_tab, "Saloni Sunil Dayma")

    zip_bytes = _create_test_zip({
        f"641_PROFILE_IMAGE_PRN_No_\t{p_tab}_Name_Saloni Sunil Dayma.JPEG": b"tab_photo_bytes",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[p_tab])

    assert report.matched_count == 1
    with engine.connect() as conn:
        photo_path = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"),
            {"p": p_tab}
        ).scalar()
        assert photo_path is not None
        p = pathlib.Path(photo_path)
        assert p.exists()
        assert "\t" not in p.name
        assert p_tab in p.name



# --------------------------------------------------------------------------- #
# 12. Path storage format: must be POSIX-relative, no backslashes (fix)
# --------------------------------------------------------------------------- #

def test_import_stores_posix_relative_path(engine, tmp_path):
    """import_photos_from_zip stores a POSIX path: no backslashes regardless of dest_dir location."""
    import uuid as _uuid
    import pathlib as _pl
    prn = f"PRN_FMT_{_uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        _insert_student(conn, prn, "Format Test Student")

    zip_bytes = _create_test_zip({
        f"1_PROFILE_IMAGE_PRN_No_{prn}_Name_Format Test Student.jpg": b"img_bytes",
    })

    dest_dir = tmp_path / "photos"
    report = import_photos_from_zip(zip_bytes, engine, dest_dir=dest_dir, prn_filter=[prn])
    assert report.matched_count == 1

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT photo_path FROM students WHERE prn = :p"), {"p": prn}
        ).scalar()

    assert stored is not None, "photo_path should be set"
    # Core fix: no Windows backslashes must ever be stored
    assert "\\" not in stored, f"photo_path contains backslash: {stored!r}"
    # Path must be POSIX-style (forward slashes)
    assert "/" in stored, f"photo_path has no forward slashes: {stored!r}"


# --------------------------------------------------------------------------- #
# 13. passes._prepare_photo resolves relative paths and normalises backslashes
# --------------------------------------------------------------------------- #

def test_prepare_photo_resolves_relative_path():
    """_prepare_photo resolves 'photos/foo.jpg' against the project root."""
    import io as _io, uuid as _uuid
    from PIL import Image
    from backend.passes import _prepare_photo, _PROJECT_ROOT
    photo_dir = _PROJECT_ROOT / "photos"
    photo_dir.mkdir(exist_ok=True)
    fname = f"_test_{_uuid.uuid4().hex[:8]}.jpg"
    photo_file = photo_dir / fname
    img = Image.new("RGB", (1, 1), color=(128, 64, 32))
    buf = _io.BytesIO(); img.save(buf, "JPEG")
    photo_file.write_bytes(buf.getvalue())
    try:
        jpeg, warning = _prepare_photo(f"photos/{fname}")
        assert warning is None, f"Expected no warning, got: {warning!r}"
        assert jpeg is not None
    finally:
        photo_file.unlink(missing_ok=True)


def test_prepare_photo_normalises_backslash_path():
    """_prepare_photo resolves backslash path on any OS."""
    import io as _io, uuid as _uuid
    from PIL import Image
    from backend.passes import _prepare_photo, _PROJECT_ROOT
    photo_dir = _PROJECT_ROOT / "photos"
    photo_dir.mkdir(exist_ok=True)
    fname = f"_test_{_uuid.uuid4().hex[:8]}.jpg"
    photo_file = photo_dir / fname
    img = Image.new("RGB", (1, 1), color=(32, 64, 128))
    buf = _io.BytesIO(); img.save(buf, "JPEG")
    photo_file.write_bytes(buf.getvalue())
    try:
        jpeg, warning = _prepare_photo("photos\\" + fname)
        assert warning is None, f"Got: {warning!r}"
        assert jpeg is not None
    finally:
        photo_file.unlink(missing_ok=True)


def test_prepare_photo_returns_no_photo_for_missing_file():
    """_prepare_photo returns NO_PHOTO for a relative path that does not exist."""
    from backend.passes import _prepare_photo
    jpeg, warning = _prepare_photo("photos/__nonexistent_xyz_test.jpg")
    assert jpeg is None
    assert warning == "NO_PHOTO"


def test_prepare_photo_returns_no_photo_for_none():
    """_prepare_photo returns NO_PHOTO when path is None."""
    from backend.passes import _prepare_photo
    jpeg, warning = _prepare_photo(None)
    assert jpeg is None
    assert warning == "NO_PHOTO"


# --------------------------------------------------------------------------- #
# 14. Migration helper _normalise covers all format variants
# --------------------------------------------------------------------------- #

def test_fix_photo_paths_normalise_windows_absolute():
    from scripts.fix_photo_paths import _normalise
    assert _normalise("E:\\JakobProjects\\1QR\\photos\\foo.jpg") == "photos/foo.jpg"


def test_fix_photo_paths_normalise_windows_relative():
    from scripts.fix_photo_paths import _normalise
    assert _normalise("photos\\foo.jpg") == "photos/foo.jpg"


def test_fix_photo_paths_normalise_already_correct():
    from scripts.fix_photo_paths import _normalise
    assert _normalise("photos/foo.jpg") is None


def test_fix_photo_paths_normalise_posix_absolute():
    from scripts.fix_photo_paths import _normalise
    assert _normalise("/app/photos/foo.jpg") == "photos/foo.jpg"

"""Phase 3 - Import, Photos, and Display Snapshot Tests.

Tests the core requirements of Phase 3:
1. Clean CSV/XLSX imports at 100% — row count in equals student count out.
2. Re-importing the identical file changes nothing (byte-identical records).
3. Re-importing a file with N new rows added to original adds exactly N new students;
   original students (same id, same token if existed) are byte-identical.
4. Duplicate PRN (within file or against existing student) is flagged with row number
   in validation preview and not written on commit.
5. Missing required field, a malformed sequence_no, or an overlong name caught in the
   validation preview before any write. A MISSING or REPEATED sequence number is only a
   note: the university's real list has no Convocation Sequence Number column.
6. Failed-validation import performs zero writes — row count before equals after.
7. Photos linked by PRN; report lists unmatched photos and students missing photos;
   placeholder returned for students with no photo.
8. After 'freeze display data' runs, the master record is locked: a plain edit is refused
   by the database, and display_snapshot moves only for another explicit freeze or for a
   logged master patch (tests/test_master_patch.py covers the patch itself).
9. Master pack exported from one venue database and imported into a second empty
   database produces an identical student set.
"""
import io
import os
import pathlib
import shutil
import tempfile
import uuid
import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL
from tests.test_schema import drop_everything, run_alembic

from backend.importer import (
    parse_file,
    detect_column_mapping,
    validate_import,
    commit_import,
    MAX_NAME_LENGTH,
)
from backend.photos import link_photos_by_prn, resolve_student_photo
from backend.snapshot import begin_master_patch_txn, freeze_display_data
from backend.master_pack import export_master_pack, import_master_pack





@pytest.fixture(autouse=True)
def clean_db(test_engine):
    """Clean all tables before each test while respecting foreign keys and triggers."""
    drop_everything(test_engine)
    res = run_alembic("upgrade", "head")
    assert res.returncode == 0
    yield test_engine


def _sample_dataframe(count=10, start_seq=1, prn_prefix="PRN"):
    rows = []
    for i in range(1, count + 1):
        seq = start_seq + i - 1
        rows.append({
            "PRN": f"{prn_prefix}{seq:04d}",
            "Student Name": f"Student {seq:04d}",
            "Programme": "B.Tech Computer Science",
            "School": "School of Engineering",
            "Convocation Sequence No": seq,
            "Seat No": f"A-{seq:03d}",
            "Awards": "First Class" if seq % 2 == 0 else None,
            "Photo": f"{prn_prefix}{seq:04d}.jpg",
        })
    return pd.DataFrame(rows)


def test_clean_csv_and_xlsx_imports_100_percent(test_engine):
    """A clean CSV/XLSX imports at 100% — row count in equals student count out."""
    df_10 = _sample_dataframe(10, start_seq=1, prn_prefix="CSV")
    csv_bytes = df_10.to_csv(index=False).encode("utf-8")

    cols, rows = parse_file(io.BytesIO(csv_bytes), "students.csv")
    mapping = detect_column_mapping(cols)

    with test_engine.connect() as conn:
        preview = validate_import(rows, mapping, conn)
        assert preview.is_valid is True
        assert len(preview.to_create) == 10
        assert len(preview.to_skip) == 0
        assert len(preview.errors) == 0

        summary = commit_import(preview, conn)
        assert summary.read == 10
        assert summary.created == 10
        assert summary.skipped == 0
        assert summary.errors == 0

        # Verify DB student count
        count = conn.execute(text("SELECT count(*) FROM students")).scalar()
        assert count == 10

        # Verify audit_log entry
        audit_entry = conn.execute(
            text("SELECT action, details FROM audit_log WHERE action = 'IMPORT_STUDENTS'")
        ).fetchone()
        assert audit_entry is not None
        assert audit_entry[1]["created"] == 10

    # Also test XLSX format with another 5 students
    df_xlsx = _sample_dataframe(5, start_seq=11, prn_prefix="XLSX")
    xlsx_buf = io.BytesIO()
    df_xlsx.to_excel(xlsx_buf, index=False, engine="openpyxl")
    xlsx_buf.seek(0)

    cols_x, rows_x = parse_file(xlsx_buf, "students.xlsx")
    mapping_x = detect_column_mapping(cols_x)

    with test_engine.connect() as conn:
        preview_x = validate_import(rows_x, mapping_x, conn)
        assert preview_x.is_valid is True
        assert len(preview_x.to_create) == 5
        summary_x = commit_import(preview_x, conn)
        assert summary_x.created == 5

        total = conn.execute(text("SELECT count(*) FROM students")).scalar()
        assert total == 15


def test_reimporting_identical_file_changes_nothing(test_engine):
    """Re-importing the identical file changes nothing."""
    df = _sample_dataframe(5, start_seq=1)
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    cols, rows = parse_file(io.BytesIO(csv_bytes), "students.csv")
    mapping = detect_column_mapping(cols)

    with test_engine.connect() as conn:
        preview1 = validate_import(rows, mapping, conn)
        commit_import(preview1, conn)

        # Snapshot the student records
        before = conn.execute(
            text("SELECT id, prn, name, programme, school, sequence_no, seat_no, awards, photo_path, status, updated_at FROM students ORDER BY sequence_no")
        ).fetchall()

        # Re-import identical file
        preview2 = validate_import(rows, mapping, conn)
        assert preview2.is_valid is True
        assert len(preview2.to_create) == 0
        assert len(preview2.to_skip) == 5
        assert len(preview2.errors) == 0

        summary2 = commit_import(preview2, conn)
        assert summary2.created == 0
        assert summary2.skipped == 5

        after = conn.execute(
            text("SELECT id, prn, name, programme, school, sequence_no, seat_no, awards, photo_path, status, updated_at FROM students ORDER BY sequence_no")
        ).fetchall()

        # Byte-identical before and after
        assert before == after


def test_reimport_with_n_new_rows_adds_only_n_new_students(test_engine):
    """Re-importing a file with N new rows added to the original adds exactly N new students;
    the original students (same id, same token if one already existed) are byte-identical before and after.
    """
    df_orig = _sample_dataframe(5, start_seq=1)
    csv_orig = df_orig.to_csv(index=False).encode("utf-8")
    cols, rows_orig = parse_file(io.BytesIO(csv_orig), "orig.csv")
    mapping = detect_column_mapping(cols)

    with test_engine.connect() as conn:
        preview_orig = validate_import(rows_orig, mapping, conn)
        commit_import(preview_orig, conn)

        first_student = conn.execute(
            text("SELECT id FROM students WHERE sequence_no = 1")
        ).fetchone()
        student_id = first_student[0]

        # Simulate issuing a QR token for student 1
        test_token = "tok_128bit_test_opaque_token_12345"
        conn.execute(
            text("INSERT INTO qr_tokens (student_id, token, active) VALUES (:sid, :tok, true)"),
            {"sid": student_id, "tok": test_token},
        )
        conn.commit()

        before_students = conn.execute(
            text("SELECT id, prn, name, sequence_no, updated_at FROM students ORDER BY sequence_no")
        ).fetchall()
        before_token = conn.execute(
            text("SELECT id, student_id, token, active FROM qr_tokens WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()

        # Create combined file with 5 original + 3 new rows (N=3)
        df_new = _sample_dataframe(8, start_seq=1)
        csv_new = df_new.to_csv(index=False).encode("utf-8")
        cols_new, rows_new = parse_file(io.BytesIO(csv_new), "combined.csv")

        preview_new = validate_import(rows_new, mapping, conn)
        assert preview_new.is_valid is True
        assert len(preview_new.to_create) == 3
        assert len(preview_new.to_skip) == 5

        summary_new = commit_import(preview_new, conn)
        assert summary_new.created == 3
        assert summary_new.skipped == 5

        # Check total count is now 8
        total = conn.execute(text("SELECT count(*) FROM students")).scalar()
        assert total == 8

        # Original students are byte-identical
        after_orig_students = conn.execute(
            text("SELECT id, prn, name, sequence_no, updated_at FROM students WHERE sequence_no <= 5 ORDER BY sequence_no")
        ).fetchall()
        assert before_students == after_orig_students

        # QR token of student 1 was untouched
        after_token = conn.execute(
            text("SELECT id, student_id, token, active FROM qr_tokens WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        assert before_token == after_token


def test_duplicate_prn_flagged_with_row_number_and_not_written(test_engine):
    """A duplicate PRN (within the file, or against an existing student) is flagged
    with its row number in the validation preview and is not written on commit.
    """
    with test_engine.connect() as conn:
        # Case A: duplicate PRN within the file
        rows = [
            {"PRN": "PRN001", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": 1},
            {"PRN": "PRN002", "Name": "Bob", "Programme": "CS", "School": "Eng", "Sequence No": 2},
            {"PRN": "PRN001", "Name": "Charlie", "Programme": "CS", "School": "Eng", "Sequence No": 3},  # duplicate PRN at row 3 (data row 3, file row 4)
        ]
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school", "Sequence No": "sequence_no"}
        preview = validate_import(rows, mapping, conn)

        assert preview.is_valid is False
        assert any(
            err.row == 3 and "Duplicate PRN" in err.message for err in preview.errors
        )
        assert any(
            dup.row == 3 and dup.prn == "PRN001" for dup in preview.flagged_duplicates
        )

        # Attempting commit must raise and write zero rows
        with pytest.raises(ValueError, match="Cannot commit an import with validation errors"):
            commit_import(preview, conn)

        assert conn.execute(text("SELECT count(*) FROM students")).scalar() == 0

        # Case B: duplicate PRN against an existing student
        # First commit student PRN001 cleanly
        clean_preview = validate_import(rows[:2], mapping, conn)
        assert clean_preview.is_valid is True
        commit_import(clean_preview, conn)
        assert conn.execute(text("SELECT count(*) FROM students")).scalar() == 2

        # Now import a file where row 1 has existing PRN001 and row 2 has new PRN003
        rows_with_existing = [
            {"PRN": "PRN001", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": 1},
            {"PRN": "PRN003", "Name": "David", "Programme": "CS", "School": "Eng", "Sequence No": 3},
        ]
        preview_existing = validate_import(rows_with_existing, mapping, conn)
        assert preview_existing.is_valid is True
        # Existing PRN is flagged in duplicates/skipped with its row number
        assert any(
            dup.row == 1 and dup.prn == "PRN001" and dup.type == "existing_student"
            for dup in preview_existing.flagged_duplicates
        )
        assert len(preview_existing.to_skip) == 1
        assert preview_existing.to_skip[0]["row"] == 1
        assert len(preview_existing.to_create) == 1
        assert preview_existing.to_create[0]["row"] == 2

        # On commit, PRN001 is NOT written (skipped), and PRN003 is written
        summary = commit_import(preview_existing, conn)
        assert summary.created == 1
        assert summary.skipped == 1
        assert conn.execute(text("SELECT count(*) FROM students")).scalar() == 3


def test_validation_catches_missing_fields_bad_sequence_no_and_overlong_name(test_engine):
    """A row with a missing required field, a sequence number that is not a positive whole number,
    or an overlong name is caught in the preview before any write.

    A MISSING or REPEATED sequence number is no longer an error: the university's real list has no
    Convocation Sequence Number column at all, and the database no longer requires the value or
    insists that it is unique.
    """
    with test_engine.connect() as conn:
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school", "Sequence No": "sequence_no"}

        # 1. Missing required field (PRN missing, Programme missing)
        rows_missing = [
            {"PRN": "", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": 1},
            {"PRN": "PRN002", "Name": "Bob", "Programme": "", "School": "Eng", "Sequence No": 2},
        ]
        preview_missing = validate_import(rows_missing, mapping, conn)
        assert preview_missing.is_valid is False
        assert any(err.row == 1 and err.field == "prn" for err in preview_missing.errors)
        assert any(err.row == 2 and err.field == "programme" for err in preview_missing.errors)

        # 2. A sequence number that is given has to be a positive whole number...
        rows_seq = [
            {"PRN": "PRN002", "Name": "Bob", "Programme": "CS", "School": "Eng", "Sequence No": "abc"},
            {"PRN": "PRN003", "Name": "Charlie", "Programme": "CS", "School": "Eng", "Sequence No": -5},
        ]
        preview_seq = validate_import(rows_seq, mapping, conn)
        assert preview_seq.is_valid is False
        assert any(err.row == 1 and err.field == "sequence_no" for err in preview_seq.errors)
        assert any(err.row == 2 and err.field == "sequence_no" for err in preview_seq.errors)

        # ...but no sequence number at all is only a note.
        no_seq = validate_import(
            [{"PRN": "PRN001", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": None}], mapping, conn)
        assert no_seq.is_valid is True

        # 3. The same sequence number twice is a note too, not a refusal
        rows_dup_seq = [
            {"PRN": "PRN001", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": 10},
            {"PRN": "PRN002", "Name": "Bob", "Programme": "CS", "School": "Eng", "Sequence No": 10},
        ]
        preview_dup_seq = validate_import(rows_dup_seq, mapping, conn)
        assert preview_dup_seq.is_valid is True

        # 4. Overlong name (> MAX_NAME_LENGTH)
        rows_long_name = [
            {"PRN": "PRN001", "Name": "A" * (MAX_NAME_LENGTH + 1), "Programme": "CS", "School": "Eng", "Sequence No": 1}
        ]
        preview_long_name = validate_import(rows_long_name, mapping, conn)
        assert preview_long_name.is_valid is False
        assert any(err.row == 1 and err.field == "name" and "overlong" in err.message.lower() for err in preview_long_name.errors)


def test_failed_validation_import_performs_zero_writes(test_engine):
    """A failed-validation import performs zero writes — check the row count before and after."""
    with test_engine.connect() as conn:
        initial_count = conn.execute(text("SELECT count(*) FROM students")).scalar()
        assert initial_count == 0

        # File with an invalid row
        rows = [
            {"PRN": "PRN001", "Name": "Alice", "Programme": "CS", "School": "Eng", "Sequence No": 1},
            {"PRN": "PRN002", "Name": "Bob", "Programme": None, "School": "Eng", "Sequence No": 2},  # Fatal error
        ]
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school", "Sequence No": "sequence_no"}
        preview = validate_import(rows, mapping, conn)
        assert preview.is_valid is False

        with pytest.raises(ValueError):
            commit_import(preview, conn)

        # Row count before and after must be exactly the same
        after_count = conn.execute(text("SELECT count(*) FROM students")).scalar()
        assert after_count == initial_count == 0


def test_photos_linked_by_prn_report_and_placeholder(test_engine):
    """Photos are linked by PRN; a report lists unmatched photos and students missing photos."""
    df = _sample_dataframe(3, start_seq=1, prn_prefix="STU")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    cols, rows = parse_file(io.BytesIO(csv_bytes), "students.csv")
    mapping = detect_column_mapping(cols)

    with test_engine.connect() as conn:
        preview = validate_import(rows, mapping, conn)
        commit_import(preview, conn)

        # Create temporary photo directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            # Student STU0001 has STU0001.jpg
            (temp_path / "STU0001.jpg").write_bytes(b"dummy_image_1")
            # Student STU0002 has STU0002.png
            (temp_path / "STU0002.png").write_bytes(b"dummy_image_2")
            # Unmatched photo (no student STU9999)
            (temp_path / "STU9999.jpg").write_bytes(b"dummy_image_unmatched")
            # Student STU0003 has no photo in directory

            report = link_photos_by_prn(temp_path, conn)

            assert report.matched_count == 2
            assert "STU9999.jpg" in [pathlib.Path(p).name for p in report.unmatched_photos]
            assert "STU0003" in [s["prn"] for s in report.unmatched_students]

            # Verify STU0001 and STU0002 photo_path updated in DB
            stu1 = conn.execute(text("SELECT photo_path FROM students WHERE prn = 'STU0001'")).scalar()
            assert stu1 is not None and "STU0001.jpg" in stu1

            stu3 = conn.execute(text("SELECT photo_path FROM students WHERE prn = 'STU0003'")).scalar()
            # Student 3 has no photo, resolve_student_photo returns placeholder
            resolved = resolve_student_photo(stu3)
            assert "placeholder" in resolved


def test_freeze_display_data_and_snapshot_immutability(test_engine):
    """After 'freeze display data' runs, the master record is locked.

    The rule Phase 3 asks for is now enforced by the database (migration 0010): once a student has
    been frozen, a plain UPDATE of their master row is refused outright, and `display_snapshot`
    moves only for another explicit freeze or for a logged master patch. So "a later master edit
    does not change the snapshot" is proved here in the strongest form available: the later master
    edit cannot happen at all unless it comes through one of those two doors.
    """
    df = _sample_dataframe(2, start_seq=1, prn_prefix="SNAP")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    cols, rows = parse_file(io.BytesIO(csv_bytes), "students.csv")
    mapping = detect_column_mapping(cols)

    with test_engine.connect() as conn:
        preview = validate_import(rows, mapping, conn)
        commit_import(preview, conn)

        student1 = conn.execute(text("SELECT id, name, awards FROM students WHERE sequence_no = 1")).fetchone()
        student_id, original_name, original_awards = student1[0], student1[1], student1[2]

        # Freeze display data
        freeze_summary = freeze_display_data(conn)
        assert freeze_summary.frozen_count == 2

        # Check display_snapshot
        snapshot1 = conn.execute(
            text("SELECT display_name, award FROM display_snapshot WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        assert snapshot1[0] == original_name
        assert snapshot1[1] == original_awards

        # A plain master edit is now refused: the student is frozen.
        with pytest.raises(Exception):
            conn.execute(
                text("UPDATE students SET name = 'Modified Name', awards = 'Modified Award' WHERE id = :sid"),
                {"sid": student_id}
            )
        conn.rollback()

        # Nothing moved: neither the master row nor the snapshot.
        assert conn.execute(text("SELECT name FROM students WHERE id = :sid"), {"sid": student_id}).scalar() == original_name
        snapshot_after_refusal = conn.execute(
            text("SELECT display_name, award FROM display_snapshot WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        assert snapshot_after_refusal[0] == original_name
        assert snapshot_after_refusal[1] == original_awards

        # The sanctioned door: the same edit, announced as a master change, is allowed...
        begin_master_patch_txn(conn)
        conn.execute(
            text("UPDATE students SET name = 'Modified Name', awards = 'Modified Award' WHERE id = :sid"),
            {"sid": student_id}
        )
        conn.commit()

        # ...and STILL does not reach display_snapshot by itself.
        snapshot_after_edit = conn.execute(
            text("SELECT display_name, award FROM display_snapshot WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        assert snapshot_after_edit[0] == original_name
        assert snapshot_after_edit[1] == original_awards

        # Only another explicit freeze updates display_snapshot
        freeze_display_data(conn)
        snapshot_after_refreeze = conn.execute(
            text("SELECT display_name, award FROM display_snapshot WHERE student_id = :sid"),
            {"sid": student_id}
        ).fetchone()
        assert snapshot_after_refreeze[0] == "Modified Name"
        assert snapshot_after_refreeze[1] == "Modified Award"


def test_master_pack_exported_and_imported_produces_identical_student_set(test_engine):
    """A master pack exported from one venue database and imported into a
    second, empty venue database produces an identical student set.
    """
    df = _sample_dataframe(5, start_seq=1, prn_prefix="PACK")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    cols, rows = parse_file(io.BytesIO(csv_bytes), "students.csv")
    mapping = detect_column_mapping(cols)

    # Use a manually-managed temp dir so pack_file outlives the export phase
    pack_tempdir = pathlib.Path(tempfile.mkdtemp())
    try:
        pack_file = pack_tempdir / "master_pack.zip"
        photos_dir = pack_tempdir / "photos"
        photos_dir.mkdir()
        (photos_dir / "sample.jpg").write_bytes(b"photo_content")

        # Phase 1: populate DB and export into pack_file
        with test_engine.connect() as conn:
            preview = validate_import(rows, mapping, conn)
            commit_import(preview, conn)
            freeze_display_data(conn)

            orig_students = conn.execute(
                text("SELECT id, prn, name, programme, school, sequence_no, seat_no, awards, photo_path, status FROM students ORDER BY sequence_no")
            ).fetchall()
            orig_snapshots = conn.execute(
                text("SELECT student_id, display_name, programme, school, award FROM display_snapshot ORDER BY student_id")
            ).fetchall()

            export_master_pack(pack_file, conn, photos_dir=photos_dir)

        assert pack_file.exists()

        # Phase 2: wipe the DB (new connection, independent of the one above)
        drop_everything(test_engine)
        res = run_alembic("upgrade", "head")
        assert res.returncode == 0

        # Phase 3: import into the freshly-rebuilt DB
        with test_engine.connect() as conn2:
            assert conn2.execute(text("SELECT count(*) FROM students")).scalar() == 0

            with tempfile.TemporaryDirectory() as target_photos_dir:
                import_master_pack(pack_file, conn2, photos_target_dir=pathlib.Path(target_photos_dir))

                imported_students = conn2.execute(
                    text("SELECT id, prn, name, programme, school, sequence_no, seat_no, awards, photo_path, status FROM students ORDER BY sequence_no")
                ).fetchall()
                imported_snapshots = conn2.execute(
                    text("SELECT student_id, display_name, programme, school, award FROM display_snapshot ORDER BY student_id")
                ).fetchall()

                assert imported_students == orig_students
                assert imported_snapshots == orig_snapshots
    finally:
        shutil.rmtree(pack_tempdir, ignore_errors=True)

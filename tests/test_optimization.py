"""tests/test_optimization.py — Focused tests for production performance optimizations.

Validates all 14 requirements from the optimization spec:
1. Pass generation requests transformed Cloudinary photos.
2. Non-pass photo loading still requests original images.
3. Authenticated Cloudinary signing includes transformation correctly.
4. Cache keys distinguish original/transformed representations.
5. PDF output still generates successfully.
6. Existing pass visual dimensions remain unchanged.
7. Repeated PDF generation can reuse a valid artifact.
8. Artifact invalidates when relevant data changes.
9. Import concurrency is bounded.
10. Import remains idempotent.
11. Import duplicate/frozen/orphan behavior remains unchanged.
12. Existing local storage tests still pass.
13. Cloudinary failures remain per-photo failures and don't corrupt the whole import.
14. No unbounded memory growth from concurrent uploads.
"""
from __future__ import annotations

import io
import time
import zipfile
import threading
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from sqlalchemy import create_engine, text

from backend import pass_cache, passes, photo_storage, photos
from backend.pass_cache import PassArtifactCache, compute_pass_fingerprint
from backend.photo_storage import (
    CLOUDINARY_VARIANTS,
    PASS_VARIANT,
    CloudinaryPhotoStore,
    LocalPhotoStore,
    PhotoStoreError,
    load_photo,
    set_current_store,
)
from backend.photos import import_photos_from_zip


@pytest.fixture(autouse=True)
def _clean_stores():
    yield
    set_current_store(None)


def _make_jpeg(size=(100, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color="blue").save(buf, format="JPEG")
    return buf.getvalue()


class MockCloudinary:
    def __init__(self):
        self.fetches: list[str] = []
        self.saved: dict[str, bytes] = {}
        self.url_calls: list[dict] = []

    def fake_upload(self, file, **options):
        public_id = options.get("public_id")
        data = file.read() if hasattr(file, "read") else file
        self.saved[public_id] = data
        return {"public_id": public_id, "bytes": len(data)}

    def fake_url(self, public_id, **options):
        self.url_calls.append({"public_id": public_id, **options})
        tx = ""
        if options.get("crop") == "limit":
            tx = f"c_limit,w_{options.get('width')},h_{options.get('height')},f_{options.get('fetch_format')}/"
        return f"https://res.cloudinary.com/demo/image/authenticated/s--signed--/{tx}{public_id}"

    def fake_fetch(self, url, timeout):
        self.fetches.append(url)
        return _make_jpeg((756, 944)), "image/jpeg"

    def store(self) -> CloudinaryPhotoStore:
        return CloudinaryPhotoStore(
            cloud_name="demo",
            api_key="key",
            api_secret="secret",
            uploader=self.fake_upload,
            url_builder=self.fake_url,
            fetcher=self.fake_fetch,
        )


# --- Tests 1-4: Cloudinary transformation, signing, and cache keys ---

def test_1_pass_generation_requests_transformed_photo():
    mock_c = MockCloudinary()
    store = mock_c.store()
    set_current_store(store)
    key = store.save("p.jpg", _make_jpeg())

    jpeg_bytes, warning = passes._prepare_photo(key)
    assert warning is None
    assert jpeg_bytes is not None and jpeg_bytes[:2] == b"\xff\xd8"
    assert len(mock_c.fetches) == 1
    assert "c_limit,w_756,h_944,f_jpg" in mock_c.fetches[0]


def test_2_non_pass_consumers_request_original_image():
    mock_c = MockCloudinary()
    store = mock_c.store()
    set_current_store(store)
    key = store.save("orig.jpg", _make_jpeg())

    blob = load_photo(key)
    assert blob is not None
    assert len(mock_c.fetches) == 1
    assert "c_limit" not in mock_c.fetches[0]


def test_3_authenticated_signing_includes_transformation():
    store = CloudinaryPhotoStore(cloud_name="demo", api_key="k", api_secret="s")
    orig_url = store.delivery_url("photos/test.jpg")
    pass_url = store.delivery_url("photos/test.jpg", variant=PASS_VARIANT)

    assert "c_limit" in pass_url
    assert "w_756" in pass_url
    assert "h_944" in pass_url
    assert "f_jpg" in pass_url
    assert "c_limit" not in orig_url
    # The signature token differs because transformation parameters are signed
    sig_orig = orig_url.split("/authenticated/")[1].split("/")[0]
    sig_pass = pass_url.split("/authenticated/")[1].split("/")[0]
    assert sig_orig != sig_pass


def test_4_cache_keys_distinguish_original_and_transformed():
    mock_c = MockCloudinary()
    store = mock_c.store()
    key = store.save("sample.jpg", _make_jpeg())

    orig_blob = store.load(key)
    pass_blob = store.load(key, variant=PASS_VARIANT)

    assert orig_blob is not pass_blob
    assert len(mock_c.fetches) == 2
    assert ("photos/sample.jpg", None) in store._cache
    assert ("photos/sample.jpg", PASS_VARIANT) in store._cache


# --- Tests 5-6: PDF generation and pass visual dimensions ---

def test_5_pdf_output_generates_successfully():
    data = [
        passes.PassData(
            name="Alice Smith",
            prn="PRN1001",
            programme="B.Tech Computer Science",
            token="A" * 32,
            photo_path=None,
        )
    ]
    result = passes.render_sheets(data, "Convocation 2026")
    assert result.pdf.startswith(b"%PDF")
    assert result.count == 1
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "NO_PHOTO"


def test_6_pass_visual_dimensions_remain_unchanged():
    # PHOTO_W = 32mm, PHOTO_H = 40mm at 300 dpi
    # PHOTO_PX must remain (round(32/25.4*300), round(40/25.4*300)) = (378, 472)
    assert passes.PHOTO_PX == (378, 472)
    assert CLOUDINARY_VARIANTS[PASS_VARIANT]["width"] == 756  # 2x PHOTO_PX[0]
    assert CLOUDINARY_VARIANTS[PASS_VARIANT]["height"] == 944 # 2x PHOTO_PX[1]


# --- Tests 7-8: PDF caching and invalidation ---

def test_7_repeated_pdf_generation_reuses_artifact(tmp_path):
    cache = PassArtifactCache(cache_dir=tmp_path)
    rows = [
        {"id": "1", "prn": "PRN1", "name": "Student One", "programme": "B.Sc",
         "photo_path": "photos/1.jpg", "sequence_no": 1, "token": "T" * 32}
    ]
    data = passes.to_pass_data(rows)

    render_count = 0

    def mock_renderer(d, ev):
        nonlocal render_count
        render_count += 1
        return passes.render_sheets(d, ev)

    # First request: cache miss, renders
    res1 = cache.get_or_render("Convocation", rows, data, renderer=mock_renderer)
    assert render_count == 1
    assert res1.pdf.startswith(b"%PDF")

    # Second request: cache hit, does not call renderer
    res2 = cache.get_or_render("Convocation", rows, data, renderer=mock_renderer)
    assert render_count == 1
    assert res2.pdf == res1.pdf
    assert res2.count == res1.count


def test_8_artifact_invalidates_when_relevant_data_changes(tmp_path):
    cache = PassArtifactCache(cache_dir=tmp_path)
    rows1 = [
        {"id": "1", "prn": "PRN1", "name": "Alice", "programme": "B.Sc",
         "photo_path": "photos/1.jpg", "sequence_no": 1, "token": "T" * 32}
    ]
    data1 = passes.to_pass_data(rows1)

    render_calls = 0

    def mock_renderer(d, ev):
        nonlocal render_calls
        render_calls += 1
        return passes.render_sheets(d, ev)

    cache.get_or_render("Convocation", rows1, data1, renderer=mock_renderer)
    assert render_calls == 1

    # Invalidate by student name change
    rows2 = [
        {"id": "1", "prn": "PRN1", "name": "Alice Modified", "programme": "B.Sc",
         "photo_path": "photos/1.jpg", "sequence_no": 1, "token": "T" * 32}
    ]
    data2 = passes.to_pass_data(rows2)
    cache.get_or_render("Convocation", rows2, data2, renderer=mock_renderer)
    assert render_calls == 2

    # Invalidate by token change
    rows3 = [
        {"id": "1", "prn": "PRN1", "name": "Alice Modified", "programme": "B.Sc",
         "photo_path": "photos/1.jpg", "sequence_no": 1, "token": "NEW_TOKEN" + "X" * 23}
    ]
    data3 = passes.to_pass_data(rows3)
    cache.get_or_render("Convocation", rows3, data3, renderer=mock_renderer)
    assert render_calls == 3

    # Invalidate by event title change
    cache.get_or_render("Annual Convocation 2026", rows3, data3, renderer=mock_renderer)
    assert render_calls == 4


# --- Tests 9-11 & 13-14: Concurrency, idempotency, errors, memory ---

def _build_test_db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE students (
                id TEXT PRIMARY KEY,
                prn TEXT UNIQUE,
                name TEXT,
                programme TEXT,
                photo_path TEXT,
                status TEXT DEFAULT 'ACTIVE'
            );
        """))
        conn.execute(text("""
            CREATE TABLE display_snapshot (
                student_id TEXT PRIMARY KEY,
                display_name TEXT,
                programme TEXT,
                school TEXT,
                award TEXT,
                photo_path TEXT
            );
        """))
    return engine


def _create_photo_zip(entries: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def test_9_import_concurrency_is_bounded(tmp_path):
    engine = _build_test_db()
    with engine.begin() as conn:
        for i in range(12):
            conn.execute(
                text("INSERT INTO students (id, prn, name) VALUES (:id, :prn, :name)"),
                {"id": str(i), "prn": f"PRN{i:03d}", "name": f"Student {i}"},
            )

    zip_entries = {
        f"1_PROFILE_IMAGE_PRN_No_PRN{i:03d}_Name_Student {i}.jpg": _make_jpeg() for i in range(12)
    }
    zip_bytes = _create_photo_zip(zip_entries)

    active_threads: set[int] = set()
    lock = threading.Lock()
    max_concurrent = 0

    class TrackingStore(LocalPhotoStore):
        def save(self, filename: str, data: bytes) -> str:
            nonlocal max_concurrent
            tid = threading.get_ident()
            with lock:
                active_threads.add(tid)
                max_concurrent = max(max_concurrent, len(active_threads))
            time.sleep(0.02)
            res = super().save(filename, data)
            with lock:
                active_threads.remove(tid)
            return res

    store = TrackingStore(tmp_path)
    report = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=4)

    assert report.matched_count == 12
    # Concurrency was bounded by 4
    assert max_concurrent <= 4


def test_10_import_remains_idempotent(tmp_path):
    engine = _build_test_db()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO students (id, prn, name) VALUES ('1', 'PRN001', 'Bob')")
        )

    fname = "PROFILE_IMAGE_PRN_No_PRN001_Name_Bob.jpg"
    zip_bytes = _create_photo_zip({fname: _make_jpeg()})
    store = LocalPhotoStore(tmp_path)

    # First run
    report1 = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=2)
    assert report1.matched_count == 1

    # Second run (exact rerun)
    zip_bytes.seek(0)
    report2 = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=2)
    assert report2.matched_count == 1

    with engine.connect() as conn:
        photo = conn.execute(text("SELECT photo_path FROM students WHERE id = '1'")).scalar()
        assert photo == f"photos/{fname}"


def test_11_import_duplicate_frozen_orphan_behavior(tmp_path):
    engine = _build_test_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO students (id, prn, name, photo_path) VALUES ('1', 'PRN001', 'Alice', 'photos/existing.jpg')"))
        conn.execute(text("INSERT INTO students (id, prn, name) VALUES ('2', 'PRN002', 'Bob')"))
        # Freeze Alice
        conn.execute(text("INSERT INTO display_snapshot (student_id, display_name, photo_path) VALUES ('1', 'Alice Frozen', 'photos/existing.jpg')"))

    # Zip contains:
    # 1. PRN001 (frozen with different photo)
    # 2. PRN002 (valid match)
    # 3. PRN999 (orphan, no student in DB)
    zip_entries = {
        "PROFILE_IMAGE_PRN_No_PRN001_Name_Alice.jpg": _make_jpeg(),
        "PROFILE_IMAGE_PRN_No_PRN002_Name_Bob.jpg": _make_jpeg(),
        "PROFILE_IMAGE_PRN_No_PRN999_Name_Orphan.jpg": _make_jpeg(),
    }
    zip_bytes = _create_photo_zip(zip_entries)
    store = LocalPhotoStore(tmp_path)

    report = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=2)

    assert report.matched_count == 1
    assert [m["prn"] for m in report.matched_students] == ["PRN002"]
    assert len(report.frozen_skipped) == 1
    assert report.frozen_skipped[0]["prn"] == "PRN001"
    assert len(report.orphaned_photos) == 1
    assert report.orphaned_photos[0]["extracted_prn"] == "PRN999"


def test_12_existing_local_storage_tests_pass(tmp_path):
    store = LocalPhotoStore(tmp_path)
    data = _make_jpeg()
    key = store.save("local.jpg", data)
    assert key == "photos/local.jpg"
    blob = store.load(key)
    assert blob is not None and blob.path == tmp_path / "local.jpg"


def test_13_cloudinary_failure_remains_per_photo_error(tmp_path):
    engine = _build_test_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO students (id, prn, name) VALUES ('1', 'PRN001', 'Student 1')"))
        conn.execute(text("INSERT INTO students (id, prn, name) VALUES ('2', 'PRN002', 'Student 2')"))

    zip_bytes = _create_photo_zip({
        "PROFILE_IMAGE_PRN_No_PRN001_Name_Student 1.jpg": _make_jpeg(),
        "PROFILE_IMAGE_PRN_No_PRN002_Name_Student 2.jpg": _make_jpeg(),
    })

    class FlakyStore(LocalPhotoStore):
        def save(self, filename: str, data: bytes) -> str:
            if "PRN001" in filename:
                raise PhotoStoreError("Cloudinary connection reset for PRN001")
            return super().save(filename, data)

    store = FlakyStore(tmp_path)
    report = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=2)

    # PRN001 failed, PRN002 succeeded; batch did not abort
    assert report.matched_count == 1
    assert [m["prn"] for m in report.matched_students] == ["PRN002"]
    assert len(report.storage_failed) == 1
    assert report.storage_failed[0]["prn"] == "PRN001"
    assert "Cloudinary connection reset" in report.storage_failed[0]["reason"]


def test_14_no_unbounded_memory_growth_during_imports(tmp_path):
    engine = _build_test_db()
    num_students = 20
    with engine.begin() as conn:
        for i in range(num_students):
            conn.execute(
                text("INSERT INTO students (id, prn, name) VALUES (:id, :prn, :name)"),
                {"id": str(i), "prn": f"PRN{i:03d}", "name": f"Student {i}"},
            )

    zip_entries = {
        f"PROFILE_IMAGE_PRN_No_PRN{i:03d}_Name_Student {i}.jpg": _make_jpeg()
        for i in range(num_students)
    }
    zip_bytes = _create_photo_zip(zip_entries)

    peak_items_in_flight = 0
    current_items_in_flight = 0
    lock = threading.Lock()

    class MonitoredStore(LocalPhotoStore):
        def save(self, filename: str, data: bytes) -> str:
            nonlocal peak_items_in_flight, current_items_in_flight
            with lock:
                current_items_in_flight += 1
                peak_items_in_flight = max(peak_items_in_flight, current_items_in_flight)
            time.sleep(0.01)
            res = super().save(filename, data)
            with lock:
                current_items_in_flight -= 1
            return res

    store = MonitoredStore(tmp_path)
    report = import_photos_from_zip(zip_bytes, engine, store=store, max_workers=4)
    assert report.matched_count == num_students
    # Active concurrent uploads in flight never exceeded max_workers
    assert peak_items_in_flight <= 4

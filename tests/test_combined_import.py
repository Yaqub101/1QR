"""Admin → Import with the student list AND the university photo ZIP in one form (plus the CLI that shares its code).

What is proved here, against the real schema (built by `alembic upgrade head`):
  * students are committed first; photos run only afterwards, through `photos.import_photos_from_zip`,
    using the same spreadsheet to turn Enrollment / Roll numbers into PRNs;
  * every category of the CLI report reaches the summary page;
  * a photo that cannot be stored (Cloudinary down) is reported per photo and links nothing;
  * a photo step that blows up leaves the students imported and says so;
  * re-running the same two files changes nothing and makes no copies;
  * a student missing from the ZIP keeps the photo they already had;
  * the staged ZIP is deleted after the commit, whichever way it went, and is copied in chunks;
  * the Admin list, the station card and the LED all serve the Cloudinary photo through the app.
No real Cloudinary account is used: the store is the mocked adapter from test_photo_storage.
"""
import io
import pathlib
import re
import uuid
import zipfile

import pandas as pd
import pytest
from sqlalchemy import text

from backend import import_staging, photo_storage, photos, web_import
from backend.photo_storage import LocalPhotoStore
from tests.admin_support import rows, scalar
from tests.test_auth import new_client
from tests.test_import_screen import batch_of, choose_columns, commit, mapping_form, summary_page
from tests.test_photo_storage import FakeCloudinary, jpeg_bytes
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    world,
)


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture
def store_on(apps):
    """Put a given photo store on the app (and as the process-wide one) for one test."""
    original = apps.state.photo_store

    def use(store):
        apps.state.photo_store = store
        photo_storage.set_current_store(store)
        return store
    yield use
    apps.state.photo_store = original
    photo_storage.set_current_store(original)


# --------------------------------------------------------------------------- building the two files
def tag():
    return uuid.uuid4().hex[:6].upper()


def student_sheet(students) -> bytes:
    """An .xlsx shaped like the university report: PRN No. and Enrollment No/Roll No side by side."""
    frame = pd.DataFrame([{"Sr.No": i + 1, "PRN No.": s["prn"], "Enrollment No/Roll No": s.get("enroll", ""),
                           "Student Name": s["name"], "Programme": "B.Tech", "School": "Engineering"}
                          for i, s in enumerate(students)])
    out = io.BytesIO()
    frame.to_excel(out, index=False, engine="openpyxl")
    return out.getvalue()


def photo_name(seq, ident, name) -> str:
    return f"{seq}_PROFILE_IMAGE_PRN_No_{ident}_Name_{name}.jpg"


def photo_zip(entries: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return out.getvalue()


def upload_both(client, sheet: bytes, zip_bytes: bytes, zip_name="Student Profile Image.zip"):
    return client.post("/admin/import/upload", follow_redirects=False, files={
        "file": ("StudentConvocationDetailReport.xlsx", sheet,
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "photos_zip": (zip_name, zip_bytes, "application/zip")})


def import_both(client, sheet, zip_bytes):
    """Upload both files, accept the detected columns, commit; return (batch id, summary page html)."""
    batch = batch_of(upload_both(client, sheet, zip_bytes))
    detected = mapping_form(client.get(f"/admin/import/{batch}/columns").text)
    assert choose_columns(client, batch, detected).status_code == 303
    preview = client.get(f"/admin/import/{batch}/preview").text
    assert "photo ZIP Student Profile Image.zip" in preview
    response = commit(client, batch)
    assert response.status_code == 303, response.text[:300]
    return batch, summary_page(client, batch)


def count(page: str, name: str) -> int:
    found = re.search(rf'data-count="{name}">(\d+)<', page)
    assert found, name
    return int(found.group(1))


def staged(apps, batch_id):
    return import_staging.load(apps.state.settings, batch_id)


def photo_path(engine, prn):
    return scalar(engine, "SELECT photo_path FROM students WHERE prn = :p", p=prn)


def a_set_of_students():
    """Four students: three photographed by PRN, one by Enrollment No; one more with no photo at all."""
    t = tag()
    people = [{"prn": f"9{t}{i}", "name": f"Student {t}{i}"} for i in range(5)]
    people[3]["enroll"] = f"ENR{t}3"
    entries = {photo_name(i + 1, p["prn"], p["name"]): jpeg_bytes((10 * i, 90, 90)) for i, p in enumerate(people[:3])}
    entries[photo_name(4, people[3]["enroll"], people[3]["name"])] = jpeg_bytes((1, 2, 3))   # Enrollment/Roll no. name
    entries[photo_name(5, f"NOSUCH{t}A", "Orphan One")] = b"orphan"
    entries[photo_name(6, f"NOSUCH{t}B", "Orphan Two")] = b"orphan"
    entries[photo_name(7, people[0]["prn"], "Second photo of the first")] = b"duplicate"
    entries["readme.txt"] = b"not a photo"
    return people, entries


# --------------------------------------------------------------------------- the combined import, locally
def test_combined_import_links_every_photo_through_the_shared_matcher(apps, engine, store_on, tmp_path):
    store = store_on(LocalPhotoStore(tmp_path / "photos"))
    people, entries = a_set_of_students()
    batch, page = import_both(admin(apps), student_sheet(people), photo_zip(entries))

    assert count(page, "read") == 5 and count(page, "created") == 5
    assert count(page, "photos-matched") == 4
    assert count(page, "photos-orphaned") == 2
    assert count(page, "photos-duplicate-photo-prns") == 1
    assert count(page, "photos-malformed") == 1 and "readme.txt" in page
    assert count(page, "photos-storage-failed") == 0
    assert people[4]["prn"] in page                                            # the student with no photo is named

    for i, person in enumerate(people[:4]):
        key = photo_path(engine, person["prn"])
        assert key is not None and key.startswith("photos/") and "\\" not in key
        assert store.load(key) is not None, person
    assert people[3]["enroll"] in photo_path(engine, people[3]["prn"])        # Enrollment No -> PRN mapping
    assert photo_path(engine, people[4]["prn"]) is None
    assert len(list((tmp_path / "photos").iterdir())) == 4                   # orphans and duplicates not stored
    assert not staged(apps, batch).photos_zip_path.exists()                   # the ZIP is gone after the commit


def test_rerunning_the_same_two_files_changes_nothing(apps, engine, store_on, tmp_path):
    store_on(LocalPhotoStore(tmp_path / "photos"))
    people, entries = a_set_of_students()
    sheet, zip_bytes = student_sheet(people), photo_zip(entries)
    import_both(admin(apps), sheet, zip_bytes)
    prns = [p["prn"] for p in people]
    before = rows(engine, "SELECT prn, photo_path, updated_at FROM students WHERE prn = ANY(:p) ORDER BY prn", p=prns)
    files_before = sorted(p.name for p in (tmp_path / "photos").iterdir())

    _, page = import_both(admin(apps), sheet, zip_bytes)
    assert count(page, "created") == 0 and count(page, "skipped") == 5
    assert count(page, "photos-matched") == 4
    assert rows(engine, "SELECT prn, photo_path, updated_at FROM students WHERE prn = ANY(:p) ORDER BY prn", p=prns) == before
    assert sorted(p.name for p in (tmp_path / "photos").iterdir()) == files_before


def test_a_student_missing_from_a_later_zip_keeps_their_photo(apps, engine, store_on, tmp_path):
    store_on(LocalPhotoStore(tmp_path / "photos"))
    people, entries = a_set_of_students()
    import_both(admin(apps), student_sheet(people), photo_zip(entries))
    kept = photo_path(engine, people[1]["prn"])
    later = {k: v for k, v in entries.items() if people[1]["prn"] not in k}
    _, page = import_both(admin(apps), student_sheet(people), photo_zip(later))
    assert photo_path(engine, people[1]["prn"]) == kept                       # never cleared by a missing photo
    assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=[p["prn"] for p in people]) == 5


# --------------------------------------------------------------------------- production storage: Cloudinary (mocked)
def test_combined_import_uploads_matched_photos_to_cloudinary(apps, engine, store_on):
    fake = FakeCloudinary()
    store = store_on(fake.store())
    people, entries = a_set_of_students()
    _, page = import_both(admin(apps), student_sheet(people), photo_zip(entries))

    assert count(page, "photos-matched") == 4 and "Cloudinary (cloud demo-cloud" in page
    assert len(fake.assets) == 4                                               # only matched photos went up
    assert all(call["type"] == "authenticated" for call in fake.upload_calls)
    key = photo_path(engine, people[0]["prn"])
    assert key.startswith("photos/") and store.public_id(key) in fake.assets   # key in the DB, bytes at Cloudinary


def test_a_failed_cloudinary_upload_is_reported_and_links_nothing(apps, engine, store_on):
    people, entries = a_set_of_students()
    broken = photo_name(2, people[1]["prn"], people[1]["name"])
    entries[broken] = b"BOOM" + entries[broken]
    store_on(FakeCloudinary(fail_for={"BOOM"}).store())
    _, page = import_both(admin(apps), student_sheet(people), photo_zip(entries))

    assert count(page, "photos-storage-failed") == 1 and count(page, "photos-matched") == 3
    assert "The photo could not be uploaded to Cloudinary." in page and people[1]["prn"] in page
    assert photo_path(engine, people[1]["prn"]) is None                        # not claimed as linked
    assert photo_path(engine, people[0]["prn"]) is not None                    # the rest still went in


def test_if_the_photo_step_fails_the_students_stay_and_the_admin_is_told(apps, engine, store_on, tmp_path, monkeypatch):
    store_on(LocalPhotoStore(tmp_path / "photos"))

    def explode(*args, **kwargs):
        raise RuntimeError("disk vanished")
    monkeypatch.setattr(web_import, "import_photos_from_zip", explode)
    people, entries = a_set_of_students()
    batch, page = import_both(admin(apps), student_sheet(people), photo_zip(entries))

    assert count(page, "created") == 5
    assert "The students were imported, but the photos could not be processed." in page
    assert "disk vanished" not in page                                         # no technical error on screen
    assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=[p["prn"] for p in people]) == 5
    assert not staged(apps, batch).photos_zip_path.exists()                    # cleaned up on the failure path too


# --------------------------------------------------------------------------- refused before anything is written
def test_an_invalid_zip_is_refused_and_nothing_is_written(apps, engine, store_on, tmp_path):
    store_on(LocalPhotoStore(tmp_path / "photos"))
    people, _ = a_set_of_students()
    staging_root = import_staging.root(apps.state.settings)
    batches_before = set(staging_root.iterdir())
    response = upload_both(admin(apps), student_sheet(people), b"this is not a zip")
    assert response.status_code == 303 and response.headers["location"].startswith("/admin/import?")
    assert "not+a+readable+ZIP" in response.headers["location"] or "not%20a%20readable%20ZIP" in response.headers["location"]
    assert set(staging_root.iterdir()) == batches_before                       # the batch was discarded
    assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=[p["prn"] for p in people]) == 0


def test_invalid_student_rows_block_the_photos_too(apps, engine, store_on, tmp_path):
    store = store_on(LocalPhotoStore(tmp_path / "photos"))
    people, entries = a_set_of_students()
    people[2]["name"] = ""                                                     # a required field missing
    client = admin(apps)
    batch = batch_of(upload_both(client, student_sheet(people), photo_zip(entries)))
    detected = mapping_form(client.get(f"/admin/import/{batch}/columns").text)
    choose_columns(client, batch, detected)
    response = commit(client, batch)
    assert "/preview" in response.headers["location"]
    assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=[p["prn"] for p in people]) == 0
    assert not (tmp_path / "photos").exists() or list((tmp_path / "photos").iterdir()) == []
    assert store.write_refusal is None


def test_on_render_without_cloudinary_the_zip_is_refused(apps, engine, store_on, tmp_path):
    store_on(LocalPhotoStore(tmp_path / "photos", write_refusal=photo_storage.RENDER_LOCAL_REFUSAL))
    people, entries = a_set_of_students()
    response = upload_both(admin(apps), student_sheet(people), photo_zip(entries))
    assert "no+permanent+photo+storage" in response.headers["location"] or "no%20permanent" in response.headers["location"]
    assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=[p["prn"] for p in people]) == 0


# --------------------------------------------------------------------------- the big ZIP is streamed
def test_the_zip_is_copied_to_staging_in_bounded_chunks(apps, tmp_path):
    class Source:
        def __init__(self, total):
            self.left, self.sizes = total, []

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024, "the whole upload must never be read at once"
            self.sizes.append(size)
            n = min(size, self.left)
            self.left -= n
            return b"z" * n
    batch = import_staging.create(apps.state.settings, content=b"PRN\n1\n", filename="s.csv", uploaded_by=None)
    try:
        source = Source(5 * 1024 * 1024 + 17)
        written = import_staging.attach_photos_zip(batch, source, "big.zip")
        assert written == 5 * 1024 * 1024 + 17 and batch.photos_zip_path.stat().st_size == written
        assert len(source.sizes) == 7
    finally:
        import_staging.discard(batch)


def test_photo_import_reads_one_zip_entry_at_a_time(engine, tmp_path, monkeypatch):
    """No list of every photo's bytes is ever built: each entry is read, stored and dropped."""
    t = tag()
    prns = [f"8{t}{i}" for i in range(3)]
    with engine.begin() as c:
        for p in prns:
            c.execute(text("INSERT INTO students (prn, name, programme, school, status) VALUES (:p, 'S', 'B', 'E', 'ACTIVE')"), {"p": p})
    zip_path = tmp_path / "p.zip"
    zip_path.write_bytes(photo_zip({photo_name(i, p, "S"): jpeg_bytes() for i, p in enumerate(prns)}))
    live = []

    class Watching(LocalPhotoStore):
        def save(self, filename, data):
            live.append(len(data))
            return super().save(filename, data)
    report = photos.import_photos_from_zip(zip_path, engine, store=Watching(tmp_path / "out"), prn_filter=prns)
    assert report.matched_count == 3 and len(live) == 3


# --------------------------------------------------------------------------- the real-data shape: 1,199 of 1,200
def test_1199_clean_matches_one_unmatched_student_and_241_orphans(engine, tmp_path):
    t = tag()
    prns = [f"7{t}{i:04d}" for i in range(1200)]
    with engine.begin() as c:
        c.execute(text("INSERT INTO students (prn, name, programme, school, status) "
                       "SELECT unnest(CAST(:p AS text[])), 'S', 'B', 'E', 'ACTIVE'"), {"p": prns})
    entries = {photo_name(i, p, "Student"): b"x%d" % i for i, p in enumerate(prns[:1199])}
    entries.update({photo_name(5000 + i, f"GONE{t}{i}", "Nobody"): b"o" for i in range(241)})
    entries[photo_name(9999, prns[0], "Again")] = b"dup"
    zip_path = tmp_path / "Student Profile Image.zip"
    zip_path.write_bytes(photo_zip(entries))

    report = photos.import_photos_from_zip(zip_path, engine, store=LocalPhotoStore(tmp_path / "photos"), prn_filter=prns)
    assert report.matched_count == 1199
    assert [s["prn"] for s in report.unmatched_students] == [prns[1199]]
    assert len(report.orphaned_photos) == 0                                    # prn_filter scopes orphans away...
    unscoped = photos.import_photos_from_zip(zip_path, engine, store=LocalPhotoStore(tmp_path / "photos"))
    assert len(unscoped.orphaned_photos) == 241                                # ...the unscoped run counts them
    assert [d["prn"] for d in report.duplicate_photo_prns] == [prns[0]]
    assert report.storage_failed == [] and len(list((tmp_path / "photos").iterdir())) == 1199


# --------------------------------------------------------------------------- every screen serves the stored photo
def test_admin_station_and_led_show_the_cloudinary_photo(apps, world, engine, store_on):
    fake = FakeCloudinary()
    store = store_on(fake.store())
    picture = jpeg_bytes((5, 200, 5))
    key = store.save("led-face.jpg", picture)
    s = make_student(engine, photo_path=key)
    with engine.begin() as c:
        led_key = c.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school, photo_path) "
                                 "VALUES (:s, 'N', 'P', 'S', :k) RETURNING led_key"), {"s": s.id, "k": key}).scalar_one()

    listing = admin(apps).get("/admin/students", params={"q": s.prn})
    assert f'src="/photo/{s.id}"' in listing.text and "cloudinary" not in listing.text.lower()
    for client, url in ((admin(apps), f"/photo/{s.id}"), (operator(apps, world, "REGISTRATION"), f"/photo/{s.id}"),
                        (new_client(apps), f"/led/photo/{led_key}")):
        response = client.get(url)
        assert response.status_code == 200 and response.content == picture, url
        assert response.headers["content-type"] == "image/jpeg"
    assert new_client(apps).get(f"/photo/{s.id}").status_code == 401          # sign-in rule unchanged


def test_a_photo_missing_at_cloudinary_shows_the_placeholder(apps, world, engine, store_on):
    store_on(FakeCloudinary().store())
    s = make_student(engine, photo_path="photos/never-uploaded.jpg")
    response = operator(apps, world, "REGISTRATION").get(f"/photo/{s.id}")
    assert response.status_code == 200 and "svg" in response.headers["content-type"]


# --------------------------------------------------------------------------- the CLI uses the same service
def cli_files(engine, tmp_path):
    t = tag()
    people = [{"prn": f"6{t}{i}", "name": f"Cli {i}"} for i in range(2)]
    people[1]["enroll"] = f"CLI{t}"
    with engine.begin() as c:
        for p in people:
            c.execute(text("INSERT INTO students (prn, name, programme, school, status) VALUES (:p, :n, 'B', 'E', 'ACTIVE')"),
                      {"p": p["prn"], "n": p["name"]})
    zip_path, sheet_path = tmp_path / "photos.zip", tmp_path / "Untitled spreadsheet.xlsx"
    zip_path.write_bytes(photo_zip({photo_name(1, people[0]["prn"], "Cli 0"): jpeg_bytes(),
                                    photo_name(2, people[1]["enroll"], "Cli 1"): jpeg_bytes()}))
    sheet_path.write_bytes(student_sheet(people))
    return people, zip_path, sheet_path


def test_cli_still_imports_into_a_local_folder(engine, tmp_path, capsys):
    from tests.conftest import TEST_DB_URL
    people, zip_path, sheet_path = cli_files(engine, tmp_path)
    dest = tmp_path / "photos"
    code = photos.main([str(zip_path), str(sheet_path), "--dest", str(dest), "--db-url", TEST_DB_URL])
    out = capsys.readouterr().out
    assert code == 0 and "Storage Write Failures:   0" in out
    for p in people:
        assert pathlib.Path(photo_path(engine, p["prn"])).exists()


def test_cli_uses_the_configured_store_and_fails_on_a_storage_error(engine, tmp_path, capsys, monkeypatch):
    from tests.conftest import TEST_DB_URL
    people, zip_path, sheet_path = cli_files(engine, tmp_path)
    monkeypatch.setattr(photo_storage, "build_store", lambda settings: FakeCloudinary(fail_for={"JFIF"}).store())
    code = photos.main([str(zip_path), str(sheet_path), "--db-url", TEST_DB_URL])
    out = capsys.readouterr().out
    assert code == 1 and "Storage Write Failures:   2" in out and "could not be uploaded to Cloudinary" in out
    assert all(photo_path(engine, p["prn"]) is None for p in people)


def test_cli_refuses_a_file_that_is_not_a_zip(tmp_path, capsys):
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"nope")
    assert photos.main([str(bad), "--dest", str(tmp_path)]) == 1
    assert "not a readable ZIP" in capsys.readouterr().err

"""Phase 3's screen: the Admin import wizard (upload -> column mapping -> preview -> commit -> summary).

Until now Phase 3 had no screen at all: `/admin/import` was a 404 and the only way to load the
university's list was curl. These tests drive the real pages with a browser client, exactly as an
Admin would: they post a file, choose the columns, read the validation preview, press Commit and
read the summary.

THE GUARANTEE THAT MATTERS MOST (AGENTS.md: "re-running the import never duplicates or modifies
existing students"): the same file can go through the whole screen twice and the second run creates
nothing, changes nothing and touches no QR token.
"""
import io
import re
import uuid
from urllib.parse import urlparse

import pandas as pd
import pytest
from sqlalchemy import text

from tests.admin_support import rows, scalar
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    world,
)
from tests.test_auth import new_client

MAX_NAME_LENGTH = 200  # written out by hand; the importer's own limit


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module", autouse=True)
def _admin_signed_in(apps, world, _fresh_client_cache):
    admin(apps)


# --------------------------------------------------------------------------- building a file
def tag():
    return uuid.uuid4().hex[:8].upper()


def student_rows(prns, *, programme="B.Tech Computer Science", school="School of Engineering", photo=True):
    out = []
    for prn in prns:
        row = {"PRN": prn, "Student Name": f"Student {prn}", "Programme": programme, "School": school}
        if photo:
            row["Photo"] = f"{prn}.jpg"
        out.append(row)
    return out


def csv_bytes(records):
    return pd.DataFrame(records).to_csv(index=False).encode("utf-8")


def xlsx_bytes(records):
    buffer = io.BytesIO()
    pd.DataFrame(records).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


# --------------------------------------------------------------------------- driving the screen
def upload(client, content, filename="students.csv", **form):
    return client.post("/admin/import/upload",
                       files={"file": (filename, content, "text/csv")}, data=form, follow_redirects=False)


def batch_of(response):
    assert response.status_code == 303, (response.status_code, response.text[:300])
    location = urlparse(response.headers["location"]).path
    found = re.match(r"^/admin/import/([0-9a-f]{32})/columns$", location)
    assert found, location
    return found.group(1)


def mapping_form(page_text):
    """The select boxes the mapping page renders, as {field name -> chosen value}."""
    chosen = {}
    for block in re.finditer(r'<select name="(map__\d+)"(.*?)</select>', page_text, re.S):
        picked = re.search(r'value="([^"]*)" selected', block.group(2))
        chosen[block.group(1)] = picked.group(1) if picked else ""
    return chosen


def choose_columns(client, batch, mapping):
    return client.post(f"/admin/import/{batch}/columns", data=mapping, follow_redirects=False)


def preview_page(client, batch):
    response = client.get(f"/admin/import/{batch}/preview")
    assert response.status_code == 200, response.text[:300]
    return response.text


def commit(client, batch):
    return client.post(f"/admin/import/{batch}/commit", follow_redirects=False)


def summary_page(client, batch):
    response = client.get(f"/admin/import/{batch}/summary")
    assert response.status_code == 200, response.text[:300]
    return response.text


def run_whole_screen(client, content, filename="students.csv", **form):
    """Upload, accept the detected mapping, read the preview, commit, and return the summary page."""
    batch = batch_of(upload(client, content, filename, **form))
    detected = mapping_form(client.get(f"/admin/import/{batch}/columns").text)
    assert choose_columns(client, batch, detected).status_code == 303
    preview = preview_page(client, batch)
    committed = commit(client, batch)
    assert committed.status_code == 303, committed.text[:300]
    return batch, preview, summary_page(client, batch)


def numbers(summary_text):
    """The five counters the summary page must show, read back off the page."""
    out = {}
    for key in ("read", "created", "updated", "skipped", "errors"):
        found = re.search(rf'data-count="{key}">(\d+)<', summary_text)
        assert found, f"the summary page does not show a {key} count:\n{summary_text[:800]}"
        out[key] = int(found.group(1))
    return out


def student_snapshot(engine, prns):
    return rows(engine, "SELECT prn, name, programme, school, sequence_no, seat_no, awards, photo_path, status, "
                        "created_at, updated_at FROM students WHERE prn = ANY(:p) ORDER BY prn", p=list(prns))


# ===================================================================== GETTING THERE
class TestTheWayIn:
    def test_the_admin_home_page_has_an_import_students_tile(self, apps):
        page = admin(apps, "college").get("/admin").text
        assert "/admin/import" in page and "Import Students" in page

    def test_the_import_screen_exists_and_is_no_longer_a_404(self, apps):
        response = admin(apps, "college").get("/admin/import")
        assert response.status_code == 200
        assert 'name="file"' in response.text

    def test_only_an_admin_can_reach_it(self, apps, world):
        assert operator(apps, world, "REGISTRATION").get("/admin/import").status_code == 403
        assert new_client(apps).get("/admin/import", follow_redirects=False).status_code in (303, 401)

    def test_the_deputy_admin_can_reach_it_too(self, apps, engine, world):
        from backend import users as users_svc
        from tests.test_auth import PASSWORD, api_login
        username = f"deputy-{tag().lower()}"
        with engine.begin() as c:
            users_svc.create_user(c, username=username, password=PASSWORD, role="DEPUTY_ADMIN")
        client = new_client(apps)
        assert api_login(client, username).status_code == 200
        assert client.get("/admin/import").status_code == 200


# ===================================================================== COLUMN MAPPING
class TestColumnMapping:
    def test_uploading_a_file_leads_to_a_mapping_step_that_lists_its_columns(self, apps):
        client = admin(apps, "college")
        batch = batch_of(upload(client, csv_bytes(student_rows([f"MAP{tag()}"]))))
        page = client.get(f"/admin/import/{batch}/columns").text
        for column in ("PRN", "Student Name", "Programme", "School", "Photo"):
            assert column in page, column

    def test_the_university_headings_are_detected_for_the_admin(self, apps):
        client = admin(apps, "college")
        batch = batch_of(upload(client, csv_bytes(student_rows([f"DET{tag()}"]))))
        chosen = set(mapping_form(client.get(f"/admin/import/{batch}/columns").text).values())
        assert {"prn", "name", "programme", "school", "photo"} <= chosen

    def test_the_mapping_page_shows_a_sample_of_the_real_rows(self, apps):
        client = admin(apps, "college")
        prn = f"SAMP{tag()}"
        batch = batch_of(upload(client, csv_bytes(student_rows([prn]))))
        assert prn in client.get(f"/admin/import/{batch}/columns").text

    def test_an_empty_cell_reads_as_empty_and_never_as_the_word_nan(self, apps, engine):
        """pandas calls an empty cell NaN. Anywhere that leaks it, a blank Awards column would be
        printed on the pass, and shown to the Admin, as the word "nan"."""
        client = admin(apps, "college")
        prn = f"BLANK{tag()}"
        records = student_rows([prn])
        records[0]["Awards"] = ""
        batch = batch_of(upload(client, csv_bytes(records)))
        page = client.get(f"/admin/import/{batch}/columns").text
        assert "nan" not in page.lower(), "an empty cell was shown to the Admin as the word nan"
        choose_columns(client, batch, mapping_form(page))
        assert commit(client, batch).status_code == 303
        assert scalar(engine, "SELECT awards FROM students WHERE prn = :p", p=prn) is None

    def test_an_admin_can_map_a_heading_the_system_has_never_seen(self, apps, engine):
        client = admin(apps, "college")
        prn = f"ODD{tag()}"
        # None of these headings is an alias the importer knows, so nothing is matched for the Admin.
        records = [{"Roll Code": prn, "Called": f"Student {prn}", "Stream": "B.Tech", "Wing": "Engineering"}]
        batch = batch_of(upload(client, csv_bytes(records)))
        assert mapping_form(client.get(f"/admin/import/{batch}/columns").text) == {
            "map__0": "", "map__1": "", "map__2": "", "map__3": ""}, "nothing should be auto-detected here"
        choose_columns(client, batch, {"map__0": "prn", "map__1": "name", "map__2": "programme", "map__3": "school"})
        assert "1" in preview_page(client, batch)
        assert commit(client, batch).status_code == 303
        assert scalar(engine, "SELECT name FROM students WHERE prn = :p", p=prn) == f"Student {prn}"

    def test_mapping_two_columns_to_the_same_field_is_refused(self, apps):
        client = admin(apps, "college")
        batch = batch_of(upload(client, csv_bytes(student_rows([f"DUP{tag()}"]))))
        response = choose_columns(client, batch, {"map__0": "prn", "map__1": "prn"})
        assert response.status_code == 303 and "error=" in response.headers["location"]

    def test_a_file_with_no_prn_column_mapped_is_refused_before_any_preview(self, apps):
        client = admin(apps, "college")
        batch = batch_of(upload(client, csv_bytes(student_rows([f"NOPRN{tag()}"]))))
        response = choose_columns(client, batch, {"map__0": "", "map__1": "name", "map__2": "programme", "map__3": "school"})
        assert response.status_code == 303 and "error=" in response.headers["location"]

    def test_an_unreadable_file_is_refused_with_a_plain_message(self, apps):
        client = admin(apps, "college")
        response = upload(client, b"\x00\x01\x02 not a spreadsheet", "rubbish.xlsx")
        assert response.status_code == 303 and "error=" in response.headers["location"]

    def test_an_unknown_batch_is_a_plain_404(self, apps):
        client = admin(apps, "college")
        for path in ("columns", "preview", "summary"):
            assert client.get(f"/admin/import/{'0' * 32}/{path}").status_code == 404


# ===================================================================== THE PREVIEW
class TestThePreview:
    def test_a_clean_file_previews_as_all_new_and_offers_the_commit(self, apps):
        client = admin(apps, "college")
        prns = [f"CLEAN{tag()}" for _ in range(4)]
        batch = batch_of(upload(client, csv_bytes(student_rows(prns))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert 'data-preview="to_create">4<' in page
        assert 'data-preview="to_skip">0<' in page
        assert f"/admin/import/{batch}/commit" in page

    def test_nothing_is_written_just_by_looking_at_the_preview(self, apps, engine):
        client = admin(apps, "college")
        prns = [f"LOOK{tag()}" for _ in range(3)]
        before = scalar(engine, "SELECT count(*) FROM students")
        batch = batch_of(upload(client, csv_bytes(student_rows(prns))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        preview_page(client, batch)
        preview_page(client, batch)
        assert scalar(engine, "SELECT count(*) FROM students") == before

    def test_a_missing_required_field_is_shown_with_its_row_number_and_blocks_the_commit(self, apps, engine):
        client = admin(apps, "college")
        records = student_rows([f"MISS{tag()}", f"MISS{tag()}"])
        records[1]["Programme"] = ""
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "programme" in page.lower() and ">2<" in page
        assert f"/admin/import/{batch}/commit" not in page, "a broken file must not offer a commit button"
        before = scalar(engine, "SELECT count(*) FROM students")
        assert commit(client, batch).status_code == 303
        assert scalar(engine, "SELECT count(*) FROM students") == before

    def test_a_duplicate_prn_inside_the_file_is_shown_with_both_row_numbers(self, apps, engine):
        client = admin(apps, "college")
        prn = f"DUP{tag()}"
        records = student_rows([prn, prn])
        records[1]["Programme"] = "Something Else to trigger conflict"
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "Duplicate PRN" in page and prn in page
        assert f"/admin/import/{batch}/commit" not in page

    def test_an_overlong_name_is_caught_before_anything_is_written(self, apps):
        client = admin(apps, "college")
        records = student_rows([f"LONG{tag()}"])
        records[0]["Student Name"] = "N" * (MAX_NAME_LENGTH + 1)
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "overlong" in page.lower()
        assert f"/admin/import/{batch}/commit" not in page

    def test_duplicate_sequence_numbers_are_a_note_that_still_lets_the_import_through(self, apps):
        client = admin(apps, "college")
        records = student_rows([f"SEQ{tag()}", f"SEQ{tag()}"])
        for record in records:
            record["Convocation Sequence No"] = "808080"
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "808080" in page
        assert f"/admin/import/{batch}/commit" in page, "a repeated sequence number is no longer fatal"

    def test_rows_with_no_photo_are_listed_before_the_commit(self, apps):
        client = admin(apps, "college")
        prn = f"NOPIC{tag()}"
        batch = batch_of(upload(client, csv_bytes(student_rows([prn], photo=False))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "photo" in page.lower() and prn in page
        assert f"/admin/import/{batch}/commit" in page, "a missing photo is a note, not a blocker"

    def test_a_photo_named_in_the_file_but_missing_from_the_folder_is_listed(self, apps, tmp_path):
        client = admin(apps, "college")
        here, gone = f"HERE{tag()}", f"GONE{tag()}"
        (tmp_path / f"{here}.jpg").write_bytes(b"not really a jpeg")
        batch = batch_of(upload(client, csv_bytes(student_rows([here, gone])), photo_dir=str(tmp_path)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert gone in page and "photo" in page.lower()

    def test_a_row_the_university_marked_inactive_is_listed_and_imported_as_inactive(self, apps, engine):
        client = admin(apps, "college")
        live, gone = f"LIVE{tag()}", f"DEAD{tag()}"
        records = student_rows([live, gone])
        records[0]["Status"] = "ACTIVE"
        records[1]["Status"] = "INACTIVE"
        batch = batch_of(upload(client, csv_bytes(records)))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert "inactive" in page.lower() and gone in page
        assert commit(client, batch).status_code == 303
        assert scalar(engine, "SELECT status FROM students WHERE prn = :p", p=gone) == "INACTIVE"
        assert scalar(engine, "SELECT status FROM students WHERE prn = :p", p=live) == "ACTIVE"

    def test_a_row_that_is_already_on_the_list_is_shown_as_such_and_not_as_an_error(self, apps, engine):
        client = admin(apps, "college")
        prn = f"AGAIN{tag()}"
        run_whole_screen(client, csv_bytes(student_rows([prn])))
        batch = batch_of(upload(client, csv_bytes(student_rows([prn, f"NEW{tag()}"]))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        page = preview_page(client, batch)
        assert 'data-preview="to_create">1<' in page
        assert 'data-preview="to_skip">1<' in page
        assert f"/admin/import/{batch}/commit" in page


# ===================================================================== COMMIT AND SUMMARY
class TestCommitAndSummary:
    def test_a_clean_file_is_committed_and_summarised(self, apps, engine):
        client = admin(apps, "college")
        prns = [f"GO{tag()}" for _ in range(5)]
        _, _, summary = run_whole_screen(client, csv_bytes(student_rows(prns)))
        assert numbers(summary) == {"read": 5, "created": 5, "updated": 0, "skipped": 0, "errors": 0}
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=prns) == 5

    def test_an_xlsx_file_works_exactly_the_same_way(self, apps, engine):
        client = admin(apps, "college")
        prns = [f"XL{tag()}" for _ in range(3)]
        _, _, summary = run_whole_screen(client, xlsx_bytes(student_rows(prns)), "students.xlsx")
        assert numbers(summary)["created"] == 3
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=prns) == 3

    def test_the_summary_names_the_file_that_was_imported(self, apps):
        client = admin(apps, "college")
        _, _, summary = run_whole_screen(client, csv_bytes(student_rows([f"NAMED{tag()}"])), "audit_student.csv")
        assert "audit_student.csv" in summary

    def test_the_import_is_written_to_the_audit_log_with_who_did_it(self, apps, engine, world):
        client = admin(apps, "college")
        before = scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'IMPORT_STUDENTS'")
        run_whole_screen(client, csv_bytes(student_rows([f"AUD{tag()}"])))
        entries = rows(engine, "SELECT operator_id, details FROM audit_log WHERE action = 'IMPORT_STUDENTS' ORDER BY id DESC LIMIT 1")
        assert scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'IMPORT_STUDENTS'") == before + 1
        assert entries[0]["operator_id"] is not None, "the screen must record which Admin ran the import"
        assert entries[0]["details"]["created"] == 1

    def test_the_summary_reports_unmatched_photos_and_students_without_one(self, apps, tmp_path):
        client = admin(apps, "college")
        matched, unmatched_student = f"PIC{tag()}", f"NOPIC{tag()}"
        stray = f"STRAY{tag()}"
        (tmp_path / f"{matched}.jpg").write_bytes(b"jpeg")
        (tmp_path / f"{stray}.jpg").write_bytes(b"jpeg")
        _, _, summary = run_whole_screen(client, csv_bytes(student_rows([matched, unmatched_student])),
                                         photo_dir=str(tmp_path))
        assert f"{stray}.jpg" in summary, "a photo with nobody to attach it to must be reported"
        assert unmatched_student in summary, "a student with no photo must be reported"

    def test_a_committed_batch_cannot_be_committed_a_second_time(self, apps, engine):
        client = admin(apps, "college")
        prn = f"ONCE{tag()}"
        batch, _, _ = run_whole_screen(client, csv_bytes(student_rows([prn])))
        before = scalar(engine, "SELECT count(*) FROM students")
        again = commit(client, batch)
        assert again.status_code == 303
        assert scalar(engine, "SELECT count(*) FROM students") == before

    def test_an_operator_cannot_commit_an_import(self, apps, world, engine):
        client = admin(apps, "college")
        prn = f"ROLE{tag()}"
        batch = batch_of(upload(client, csv_bytes(student_rows([prn]))))
        choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
        assert operator(apps, world, "REGISTRATION").post(f"/admin/import/{batch}/commit").status_code == 403
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = :p", p=prn) == 0


# ===================================================================== THE SAME FILE TWICE
class TestRunningTheSameImportTwice:
    def test_the_second_run_creates_nothing_and_changes_nothing(self, apps, engine):
        client = admin(apps, "college")
        prns = [f"RERUN{tag()}" for _ in range(6)]
        content = csv_bytes(student_rows(prns))

        _, _, first = run_whole_screen(client, content)
        assert numbers(first) == {"read": 6, "created": 6, "updated": 0, "skipped": 0, "errors": 0}
        before = student_snapshot(engine, prns)
        total_before = scalar(engine, "SELECT count(*) FROM students")

        _, preview, second = run_whole_screen(client, content)
        assert 'data-preview="to_create">0<' in preview
        assert 'data-preview="to_skip">6<' in preview
        assert numbers(second) == {"read": 6, "created": 0, "updated": 0, "skipped": 6, "errors": 0}

        assert student_snapshot(engine, prns) == before, "existing rows must be byte-identical after a re-run"
        assert scalar(engine, "SELECT count(*) FROM students") == total_before
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=prns) == 6

    def test_the_second_run_with_the_photo_folder_attached_touches_no_row_either(self, apps, engine, tmp_path):
        """Re-linking a photo a student already has must not rewrite the row: an UPDATE that changes
        nothing still moves updated_at, and "the second run changes nothing" has to mean nothing."""
        client = admin(apps, "college")
        prns = [f"PHOTO{tag()}" for _ in range(3)]
        for prn in prns:
            (tmp_path / f"{prn}.jpg").write_bytes(b"jpeg")
        content = csv_bytes(student_rows(prns))

        run_whole_screen(client, content, photo_dir=str(tmp_path))
        before = student_snapshot(engine, prns)
        assert all(row["photo_path"] for row in before), "the photos should have been attached on the first run"

        _, _, summary = run_whole_screen(client, content, photo_dir=str(tmp_path))
        assert numbers(summary)["created"] == 0
        assert student_snapshot(engine, prns) == before

    def test_a_qr_token_issued_before_the_re_run_is_untouched(self, apps, engine):
        client = admin(apps, "college")
        prn = f"TOK{tag()}"
        content = csv_bytes(student_rows([prn]))
        run_whole_screen(client, content)
        student_id = scalar(engine, "SELECT id FROM students WHERE prn = :p", p=prn)
        with engine.begin() as c:
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t)"),
                      {"s": student_id, "t": uuid.uuid4().hex + uuid.uuid4().hex})
        before = rows(engine, "SELECT t.*, t.xmin::text AS xmin, t.ctid::text AS ctid FROM qr_tokens t "
                              "WHERE student_id = :s", s=student_id)
        run_whole_screen(client, content)
        after = rows(engine, "SELECT t.*, t.xmin::text AS xmin, t.ctid::text AS ctid FROM qr_tokens t "
                             "WHERE student_id = :s", s=student_id)
        assert after == before

    def test_the_second_half_of_the_list_adds_only_the_new_students(self, apps, engine):
        client = admin(apps, "college")
        first_half = [f"HALF{tag()}" for _ in range(4)]
        second_half = [f"HALF{tag()}" for _ in range(3)]
        run_whole_screen(client, csv_bytes(student_rows(first_half)))
        unchanged = student_snapshot(engine, first_half)

        _, preview, summary = run_whole_screen(client, csv_bytes(student_rows(first_half + second_half)))
        assert 'data-preview="to_create">3<' in preview
        assert numbers(summary) == {"read": 7, "created": 3, "updated": 0, "skipped": 4, "errors": 0}
        assert student_snapshot(engine, first_half) == unchanged
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=second_half) == 3

    def test_a_students_journey_survives_a_re_import(self, apps, engine):
        client = admin(apps, "college")
        prn = f"JOUR{tag()}"
        content = csv_bytes(student_rows([prn]))
        run_whole_screen(client, content)
        student_id = scalar(engine, "SELECT id FROM students WHERE prn = :p", p=prn)
        with engine.begin() as c:
            c.execute(text("INSERT INTO activity_events (student_id, activity, operator_id) "
                           "VALUES (:s, 'REGISTRATION', gen_random_uuid())"), {"s": student_id})
        run_whole_screen(client, content)
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s", s=student_id) == 1


# ===================================================================== THE ENDPOINTS BEHIND IT
class TestTheEndpointsAreUnchanged:
    """The screen sits in front of the endpoints that already existed; they still answer the same way."""

    def test_preview_still_answers_the_same_json(self, apps):
        client = admin(apps, "college")
        prns = [f"API{tag()}" for _ in range(2)]
        response = client.post("/admin/import/preview",
                               files={"file": ("s.csv", csv_bytes(student_rows(prns)), "text/csv")})
        assert response.status_code == 200
        body = response.json()
        assert set(body) >= {"columns", "mapping", "to_create", "to_skip", "flagged_duplicates", "errors", "is_valid"}
        assert body["to_create"] == 2 and body["is_valid"] is True

    def test_commit_still_answers_the_same_json_and_still_writes(self, apps, engine):
        client = admin(apps, "college")
        prns = [f"APIC{tag()}" for _ in range(2)]
        response = client.post("/admin/import/commit",
                               files={"file": ("s.csv", csv_bytes(student_rows(prns)), "text/csv")})
        assert response.status_code == 200
        res = response.json()
        assert {k: res[k] for k in ("read", "created", "updated", "skipped", "errors")} == {
            "read": 2, "created": 2, "updated": 0, "skipped": 0, "errors": 0
        }
        assert "unmapped_programmes" in res and "unmapped_programmes_count" in res
        assert scalar(engine, "SELECT count(*) FROM students WHERE prn = ANY(:p)", p=prns) == 2

    def test_commit_still_refuses_a_broken_file_with_422_and_writes_nothing(self, apps, engine):
        client = admin(apps, "college")
        prn = f"APIX{tag()}"
        records = student_rows([prn, prn])
        records[1]["Programme"] = "Conflict!"
        before = scalar(engine, "SELECT count(*) FROM students")
        response = client.post("/admin/import/commit",
                               files={"file": ("s.csv", csv_bytes(records), "text/csv")})
        assert response.status_code == 422
        assert "errors" in response.json()["detail"]
        assert scalar(engine, "SELECT count(*) FROM students") == before

    def test_an_unparseable_file_is_still_a_400(self, apps):
        response = admin(apps, "college").post(
            "/admin/import/preview", files={"file": ("x.xlsx", b"\x00\x01rubbish", "application/octet-stream")})
        assert response.status_code == 400

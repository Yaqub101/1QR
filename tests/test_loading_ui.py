"""Loading states on the Admin pages: every slow action is wired to static/busy.js, and nothing else is.

static/busy.js does the work (its behaviour, including double-submit prevention and the download state, is driven
under node by tests/js/busy.test.js). What is proved here is that the pages really use it: every form that
imports, commits, generates, corrects or deletes carries `data-busy`, every file download carries
`data-download`, the long imports carry their keep-this-page-open note, and the fast GET forms (search, filters)
are left as plain navigation.
"""
import re

import pytest

from tests.test_combined_import import photo_zip, student_sheet, upload_both
from tests.test_import_screen import batch_of, choose_columns, csv_bytes, mapping_form, student_rows, upload
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    world,
)

KEEP_OPEN = "Please keep this page open. This may take several minutes."


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


def page(apps, url):
    response = admin(apps).get(url)
    assert response.status_code == 200, (url, response.status_code, response.text[:300])
    return response.text


def forms(html):
    """{action: opening <form ...> tag} for every POST form on the page."""
    found = {}
    for tag in re.findall(r"<form\b[^>]*>", html, re.S):
        if re.search(r'method="post"', tag, re.I):
            found[re.search(r'action="([^"]+)"', tag).group(1)] = tag
    return found


def attr(tag, name):
    m = re.search(rf'{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def download_links(html):
    return re.findall(r"<a\b[^>]*>", html)


def test_every_page_loads_the_loading_script(apps):
    for url in ("/admin", "/admin/import", "/admin/passes", "/admin/system", "/admin/users"):
        assert '<script src="/static/busy.js" defer></script>' in page(apps, url)
    assert admin(apps).get("/static/busy.js").status_code == 200


def test_the_spinner_style_exists(apps):
    css = admin(apps).get("/static/app.css").text
    assert ".is-busy" in css and "@keyframes busy-spin" in css and ".busy-note" in css


def test_the_import_upload_shows_a_loading_state(apps):
    tag = forms(page(apps, "/admin/import"))["/admin/import/upload"]
    assert attr(tag, "data-busy") == "Uploading and checking…"
    assert "keep this page open" in attr(tag, "data-busy-note")


def test_the_column_step_and_the_commit_show_a_loading_state(apps):
    client = admin(apps)
    batch = batch_of(upload(client, csv_bytes(student_rows(["LUI0001"]))))
    assert attr(forms(client.get(f"/admin/import/{batch}/columns").text)[f"/admin/import/{batch}/columns"], "data-busy") == "Checking…"
    choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
    tag = forms(page(apps, f"/admin/import/{batch}/preview"))[f"/admin/import/{batch}/commit"]
    assert attr(tag, "data-busy") == "Importing…"
    assert attr(tag, "data-busy-note") == f"Importing students… {KEEP_OPEN}"


def test_a_combined_student_and_photo_import_says_so(apps):
    client = admin(apps)
    people = [{"prn": "LUIZIP1", "name": "Zip Person"}]
    batch = batch_of(upload_both(client, student_sheet(people), photo_zip({"readme.txt": b"x"})))
    choose_columns(client, batch, mapping_form(client.get(f"/admin/import/{batch}/columns").text))
    tag = forms(page(apps, f"/admin/import/{batch}/preview"))[f"/admin/import/{batch}/commit"]
    assert attr(tag, "data-busy-note") == f"Importing students &amp; photos… {KEEP_OPEN}"


def test_bulk_token_generation_and_pass_downloads(apps, engine):
    make_student(engine)
    html = page(apps, "/admin/passes")
    assert attr(forms(html)["/admin/passes/generate"], "data-busy") == "Generating…"
    links = [l for l in download_links(html) if "/admin/passes/download" in l]
    assert links and all("data-download" in l for l in links)
    everything = next(l for l in links if "download_all" in l)
    assert attr(everything, "data-busy") == "Preparing all passes…"


def test_report_audit_and_exception_exports(apps):
    report = page(apps, "/admin/reports/not-attended")
    audit = page(apps, "/admin/audit")
    exceptions = page(apps, "/admin/exceptions")
    for html in (report, audit, exceptions):
        exports = [l for l in download_links(html) if "/export?" in l]
        assert exports and all("data-download" in l for l in exports), exports


def test_a_students_page_downloads_and_corrections(apps, engine):
    s = make_student(engine)
    html = page(apps, f"/admin/students/{s.id}")
    downloads = [l for l in download_links(html) if "export?" in l or "pass.pdf" in l]
    assert len(downloads) == 3 and all("data-download" in l for l in downloads)
    assert attr(forms(html)[f"/admin/students/{s.id}/reissue-qr"], "data-busy") == "Reissuing…"


def test_account_actions_including_delete(apps):
    html = page(apps, "/admin/users")
    tags = forms(html)
    assert tags and all(attr(t, "data-busy") for a, t in tags.items() if a != "/logout")
    delete = next(t for a, t in tags.items() if a.endswith("/delete"))
    assert "confirm(" in delete                 # the existing "Are you sure?" box still comes first


def test_the_reset_pages_show_a_loading_state(apps):
    html = page(apps, "/admin/system")
    assert attr(forms(html)["/admin/system/reset"], "data-busy") == "Checking…"
    confirm = admin(apps).post("/admin/system/reset", data={"phrase": "DELETE ALL DATA", "password": "Test-Pass-2026!"})
    tag = forms(confirm.text)["/admin/system/reset/execute"]
    assert attr(tag, "data-busy") == "Deleting all data…"
    assert KEEP_OPEN in attr(tag, "data-busy-note")


def test_fast_navigation_is_left_alone(apps):
    """Search and filter forms are plain GET navigation: no spinner, no disabled button, nothing to slow them."""
    for url in ("/admin/students", "/admin/audit"):
        for tag in re.findall(r"<form\b[^>]*>", page(apps, url)):
            if 'method="get"' in tag:
                assert "data-busy" not in tag, tag
    assert "data-busy" not in page(apps, "/admin").split("<main>", 1)[1]   # the home tiles are plain links

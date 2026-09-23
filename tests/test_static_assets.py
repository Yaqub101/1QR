"""Cache-busting for scripts and stylesheets.

After a deploy, a browser that still holds an old copy of e.g. static/stage.js must never keep running it: every
page links its scripts and stylesheets as /static/<file>?v=<first 10 hex of the file's SHA-256>, so a changed
file gets a new address and an unchanged one keeps its cache. No manual "reload every screen" step.
"""
import hashlib
import re

import pytest

from backend import web
from tests.test_auth import new_client
from tests.test_caller import caller  # noqa: F401  (fixture)
from tests.test_stage import _fresh_client_cache, clean_stage, stage  # noqa: F401  (fixtures)
from tests.test_station_engine import REPO_ROOT, admin, apps, engine, operator, world  # noqa: F401

STATIC = REPO_ROOT / "static"
REF = re.compile(r'(?:src|href)="(/static/[^"]+)"')


def expected_version(name: str) -> str:
    return hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]


def static_refs(html: str) -> list:
    return REF.findall(html)


@pytest.fixture
def pages(apps, world, stage, caller):
    return {
        "login": new_client(apps).get("/login"),
        "led": new_client(apps).get("/led"),
        "registry desk": operator(apps, world, "REGISTRATION").get("/station/registry"),
        "queue station": operator(apps, world, "QUEUE").get("/station/queue"),
        "stage": stage.main.get("/station/stage"),
        "caller": caller.get("/caller"),
        "admin dashboard": admin(apps).get("/admin"),
    }


def test_every_script_and_stylesheet_on_every_page_carries_its_content_version(pages):
    seen = set()
    for page, response in pages.items():
        assert response.status_code == 200, page
        refs = static_refs(response.text)
        assert refs, f"{page}: no static references found"
        for ref in refs:
            path, _, query = ref.partition("?")
            name = path[len("/static/"):]
            assert query == f"v={expected_version(name)}", f"{page}: {ref} is not versioned by its content"
            seen.add(name)
    assert {"app.css", "busy.js", "station.js", "camera_scan.js", "stage.js", "caller.js", "led.js", "led.css"} <= seen


def test_no_template_links_a_static_file_directly():
    for template in (REPO_ROOT / "templates").glob("*.html"):
        for ref in static_refs(template.read_text(encoding="utf-8")):
            pytest.fail(f"{template.name} links {ref} without asset_url(); a deploy could leave browsers on the old file")


def test_the_versioned_address_serves_the_current_file(apps, pages):
    ref = next(r for r in static_refs(pages["stage"].text) if "stage.js" in r)
    response = new_client(apps).get(ref)
    assert response.status_code == 200 and response.content == (STATIC / "stage.js").read_bytes()


def test_a_changed_file_gets_a_new_address_and_an_unchanged_one_keeps_it(tmp_path):
    (tmp_path / "a.js").write_text("one", encoding="utf-8")
    (tmp_path / "b.js").write_text("stays", encoding="utf-8")
    before = web.versioned_asset(tmp_path, "a.js"), web.versioned_asset(tmp_path, "b.js")
    (tmp_path / "a.js").write_text("two", encoding="utf-8")
    after = web.versioned_asset(tmp_path, "a.js"), web.versioned_asset(tmp_path, "b.js")
    assert after[0] != before[0] and after[1] == before[1]
    assert after[0] == "/static/a.js?v=" + hashlib.sha256(b"two").hexdigest()[:10]


def test_a_missing_asset_fails_loudly_instead_of_linking_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        web.versioned_asset(tmp_path, "missing.js")

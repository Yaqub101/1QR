"""Phase 4 - QR tokens and convocation passes.

THE RULE UNDER TEST (AGENTS.md golden rule 1, SYSTEM_SPEC 6 and 20): the QR holds ONE opaque random token and
NOTHING else - no PRN, no name, no encoded student data of any kind.

What is checked, and how it is checked without trusting the code under test:

  * Tokens: every ACTIVE student ends with exactly one active token; a second run of "generate missing" leaves the
    qr_tokens table byte-identical (row contents AND the physical tuple identity xmin/ctid, so not even a no-op
    UPDATE can hide); the token is at least 128 bits and comes straight from `secrets.token_bytes`.
  * QR images: decoded with a DIFFERENT library (zxing-cpp) from the one that draws them (segno). The pass PDF is
    RENDERED with pdfium (as a viewer or a printer's RIP would) and the QR is decoded from the rendered pixels, so
    what is asserted is what would be printed, not what the drawing code intended.
  * Print geometry: the symbol's physical size and its quiet zone are MEASURED from the rendered page.
  * Reissue: old token NOT ACTIVE (deactivated_at/by set), new active token, reason mandatory, audited, atomic.
    "Old token rejected at a station" is Phase 6 (already built); here it is checked at the token layer
    (`identify_by_token`) with one end-to-end /scan as a bonus.

PRINT ASSUMPTIONS (mirrored in backend/passes.py and the report): a printed module is 0.05 inch = 15 printer dots at
300 dpi (= 30 dots at 600 dpi) = 1.27 mm; the symbol is 29 x 29 modules (version 3, error correction Q) = 36.8 mm
square; the quiet zone is 4 modules = 5.1 mm on every side; the pass is 105 x 148.5 mm; printed at 100% ("actual size").
"""
import ast
import io
import pathlib
import re
import statistics
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

import backend.qr_tokens as qr_tokens
from backend import passes
from backend.engine import messages, pipeline
from tests.admin_support import error_code, rows, scalar
from tests.test_auth import ACTIVITIES
from tests.test_schema import _run_threads
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    STATION,
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    scan,
    world,
)

pdfium = pytest.importorskip("pypdfium2")
zxingcpp = pytest.importorskip("zxingcpp")
pypdf = pytest.importorskip("pypdf")

HEX32 = re.compile(r"^[0-9A-F]{32}$")
MM = 72 / 25.4  # PDF points per millimetre

# The physical layout, written out by hand (NOT imported from backend.passes) so the geometry is checked against the
# stated print assumptions and not against itself.
CELL_W_MM, CELL_H_MM = 105.0, 148.5       # one pass; two of them across and two down make exactly one A4 sheet
A4_W_MM, A4_H_MM = 210.0, 297.0
MODULES = 29                              # QR version 3
MODULE_MM = 15 / 300 * 25.4               # 15 printer dots at 300 dpi = 0.05 inch
QUIET_MODULES = 4                         # the QR standard's minimum quiet zone


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module", autouse=True)
def _admin_signed_in(apps, world, _fresh_client_cache):
    """Sign everyone in once, up front: signing in writes audit rows and the tests compare audit counts."""
    for venue in ("college", "central"):
        admin(apps, venue)
    for activity in ACTIVITIES:
        operator(apps, world, activity)


# ------------------------------------------------------------------------------------------------- helpers
_N = iter(range(8_000_000, 9_000_000))


def new_student(engine, *, active=True, school="School of Engineering", name=None, programme="B.Tech Computer Science",
                photo_path=None, sequence_no=None):
    """A student with NO token (the state right after an import)."""
    n = next(_N)
    with engine.begin() as c:
        sid = c.execute(
            text("INSERT INTO students (prn, name, programme, school, sequence_no, status, photo_path) "
                 "VALUES (:prn, :name, :prog, :school, :seq, :st, :photo) RETURNING id"),
            {"prn": f"Q{n}", "name": name or f"Quentin Student {n}", "prog": programme, "school": school,
             "seq": sequence_no or n, "st": "ACTIVE" if active else "INACTIVE", "photo": photo_path}).scalar_one()
    return SimpleNamespace(id=sid, prn=f"Q{n}", name=name or f"Quentin Student {n}", programme=programme, school=school)


def token_rows(engine, student=None):
    if student is None:
        return rows(engine, "SELECT * FROM qr_tokens ORDER BY id")
    return rows(engine, "SELECT * FROM qr_tokens WHERE student_id = :s ORDER BY id", s=student.id)


def active_token(engine, student) -> str:
    return scalar(engine, "SELECT token FROM qr_tokens WHERE student_id = :s AND active", s=student.id)


def token_table_fingerprint(engine):
    """Every row's content AND its physical tuple identity: an UPDATE of any kind (even one that changes nothing)
    writes a new tuple version and moves xmin/ctid, so an unchanged fingerprint means the table was not touched."""
    return scalar(engine, "SELECT md5(coalesce(string_agg(t::text || xmin::text || ctid::text, '|' ORDER BY id), '')) FROM qr_tokens t")


def students_without_exactly_one_active_token(engine):
    return rows(engine, "SELECT s.prn, (SELECT count(*) FROM qr_tokens t WHERE t.student_id = s.id AND t.active) AS n "
                        "FROM students s WHERE s.status = 'ACTIVE' AND (SELECT count(*) FROM qr_tokens t WHERE t.student_id = s.id AND t.active) <> 1")


def audit_count(engine, action=None):
    if action:
        return scalar(engine, "SELECT count(*) FROM audit_log WHERE action = :a", a=action)
    return scalar(engine, "SELECT count(*) FROM audit_log")


def write_photo(directory: pathlib.Path, name: str, *, size=(600, 800), color=(200, 30, 30), mode="RGB", fmt="JPEG"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    Image.new(mode, size, color if mode != "RGBA" else color + (255,)).save(path, fmt)
    return str(path)


# ---- rendering and decoding a PDF the way a viewer / printer would ---------------------------------------------
def render_page(pdf: bytes, page_index=0, dpi=300):
    doc = pdfium.PdfDocument(pdf)
    page = doc[page_index]
    return page.render(scale=dpi / 72).to_pil().convert("RGB")


def decode_qr(image) -> list:
    """Every QR code in the image, as the decoder reports it. Uses zxing-cpp, not the library that draws the code."""
    return [r for r in zxingcpp.read_barcodes(image) if r.format == zxingcpp.BarcodeFormat.QRCode]


def cell_box_px(row, col, dpi):
    px = dpi / 25.4
    return (round(col * CELL_W_MM * px), round(row * CELL_H_MM * px), round((col + 1) * CELL_W_MM * px), round((row + 1) * CELL_H_MM * px))


def cell_text(pdf: bytes, page_index, row, col, *, single=False) -> str:
    """The text printed inside one pass, whitespace-normalised."""
    doc = pdfium.PdfDocument(pdf)
    page = doc[page_index]
    width, height = page.get_size()
    left = col * CELL_W_MM * MM + 0.5
    top = height - row * CELL_H_MM * MM - 0.5
    got = page.get_textpage().get_text_bounded(left=left, bottom=top - CELL_H_MM * MM + 1, right=left + CELL_W_MM * MM - 1, top=top)
    return " ".join(got.split())


def shows(shown: str, expected: str) -> bool:
    """`expected` is printed on the pass, ignoring where a long value wrapped onto a new line."""
    shown = shown.replace("\x02", "-").replace("\ufffe", "-")   # pdfium reports a hyphen that ends a line as a marker character
    return re.sub(r"\s+", "", expected) in re.sub(r"\s+", "", shown)


def page_count(pdf: bytes) -> int:
    return len(pdfium.PdfDocument(pdf))


def page_size_mm(pdf: bytes, index=0):
    w, h = pdfium.PdfDocument(pdf)[index].get_size()
    return w / MM, h / MM


def embedded_images(pdf: bytes, page_index=0):
    return list(pypdf.PdfReader(io.BytesIO(pdf)).pages[page_index].images)


def pass_data(student, token, photo_path=None):
    return passes.PassData(name=student.name, prn=student.prn, programme=student.programme, photo_path=photo_path, token=token)


def make_pass(engine, tmp_path, *, name=None, photo="ok", **kw):
    s = new_student(engine, name=name, **kw)
    path = write_photo(tmp_path, f"{s.prn}.jpg") if photo == "ok" else None
    with engine.begin() as c:
        c.execute(text("UPDATE students SET photo_path = :p WHERE id = :i"), {"p": path, "i": s.id})
    qr_tokens.generate_missing_tokens(engine)
    s.photo_path, s.token = path, active_token(engine, s)
    return s


EVENT = "Annual Convocation 2026"


# ═══════════════════════════════════════════════════════════════════════════════ TOKEN GENERATION
class TestGenerateMissingTokens:
    def test_every_active_student_ends_with_exactly_one_active_token(self, engine):
        fresh = [new_student(engine) for _ in range(12)]
        already = [make_student(engine, token=True) for _ in range(3)]   # tokens that exist BEFORE generation
        inactive = [new_student(engine, active=False) for _ in range(3)]
        before = {a.id: token_rows(engine, a) for a in already}

        result = qr_tokens.generate_missing_tokens(engine)

        assert students_without_exactly_one_active_token(engine) == []
        for s in fresh:
            assert len(token_rows(engine, s)) == 1 and token_rows(engine, s)[0]["active"]
        assert result.created >= 12
        # an INACTIVE student is not issued a pass
        for s in inactive:
            assert token_rows(engine, s) == []
        # existing tokens were not touched: same rows, same values
        for a in already:
            assert token_rows(engine, a) == before[a.id]

    def test_running_it_a_second_time_changes_nothing(self, engine):
        for _ in range(8):
            new_student(engine)
        qr_tokens.generate_missing_tokens(engine)
        tokens_before = [(r["id"], r["student_id"], r["token"], r["active"]) for r in token_rows(engine)]
        fingerprint = token_table_fingerprint(engine)
        audit_before = audit_count(engine)

        again = qr_tokens.generate_missing_tokens(engine)

        tokens_after = [(r["id"], r["student_id"], r["token"], r["active"]) for r in token_rows(engine)]
        assert again.created == 0
        assert tokens_after == tokens_before                    # byte-identical values, no new rows, no churn
        assert token_table_fingerprint(engine) == fingerprint   # not one row rewritten (xmin/ctid unchanged)
        assert audit_count(engine) == audit_before              # a run that did nothing leaves no record either

    def test_a_late_arrival_gets_a_token_and_nobody_elses_changes(self, engine):
        qr_tokens.generate_missing_tokens(engine)
        others = token_table_fingerprint(engine)
        late = new_student(engine)
        result = qr_tokens.generate_missing_tokens(engine)
        assert result.created == 1 and len(token_rows(engine, late)) == 1
        untouched = scalar(engine, "SELECT md5(coalesce(string_agg(t::text || xmin::text || ctid::text, '|' ORDER BY id), '')) "
                                   "FROM qr_tokens t WHERE student_id <> :s", s=late.id)
        assert untouched == others                              # the half list imported earlier keeps its QRs

    def test_a_student_who_was_inactive_and_is_now_active_is_picked_up(self, engine):
        s = new_student(engine, active=False)
        qr_tokens.generate_missing_tokens(engine)
        assert token_rows(engine, s) == []
        with engine.begin() as c:
            c.execute(text("UPDATE students SET status = 'ACTIVE' WHERE id = :i"), {"i": s.id})
        qr_tokens.generate_missing_tokens(engine)
        assert len(token_rows(engine, s)) == 1

    def test_a_student_whose_every_token_was_deactivated_gets_a_fresh_one(self, engine, world):
        s = new_student(engine)
        qr_tokens.generate_missing_tokens(engine)
        with engine.begin() as c:
            c.execute(text("UPDATE qr_tokens SET active = false, deactivated_at = now(), deactivated_by = :u WHERE student_id = :s"),
                      {"u": world.admin_id, "s": s.id})
        qr_tokens.generate_missing_tokens(engine)
        assert [r["active"] for r in token_rows(engine, s)] == [False, True]

    def test_two_admins_pressing_the_button_at_once_still_leave_one_token_each(self, engine):
        students = [new_student(engine) for _ in range(30)]
        results = _run_threads(lambda i: qr_tokens.generate_missing_tokens(engine), 4)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert students_without_exactly_one_active_token(engine) == []
        assert all(len(token_rows(engine, s)) == 1 for s in students)   # nobody got two rows, and nobody lost theirs
        assert sum(r.created for r in results) == 30                    # each student was issued by exactly one of the runs

    def test_the_run_is_audited_with_a_count_and_never_with_a_token(self, engine, apps, world):
        for _ in range(3):
            new_student(engine)
        before = audit_count(engine, "QR_TOKENS_GENERATED")
        r = admin(apps, "college").post("/admin/api/qr/generate-missing")
        assert r.status_code == 200, r.text
        assert r.json()["created"] >= 3
        entry = rows(engine, "SELECT * FROM audit_log WHERE action = 'QR_TOKENS_GENERATED' ORDER BY id DESC LIMIT 1")[0]
        assert audit_count(engine, "QR_TOKENS_GENERATED") == before + 1
        assert entry["operator_id"] == world.admin_id and entry["details"]["created"] == r.json()["created"]
        assert not any(x["token"] in str(entry) for x in token_rows(engine))                      # no token in the log
        # ...and a second press is a no-op that reports zero and writes nothing
        again = admin(apps, "college").post("/admin/api/qr/generate-missing")
        assert again.json()["created"] == 0 and audit_count(engine, "QR_TOKENS_GENERATED") == before + 1

    def test_generation_is_admin_only(self, apps, world, engine):
        before = (scalar(engine, "SELECT count(*) FROM qr_tokens"), audit_count(engine))
        new_student(engine)
        assert operator(apps, world, "REGISTRATION").post("/admin/api/qr/generate-missing").status_code == 403
        anonymous = apps["college"]
        assert TestClient(anonymous).post("/admin/api/qr/generate-missing").status_code == 401
        assert (scalar(engine, "SELECT count(*) FROM qr_tokens"), audit_count(engine)) == before


# ═══════════════════════════════════════════════════════════════════════════════ THE TOKEN ITSELF
class TestTokenIsRandomAndAtLeast128Bits:
    def test_at_least_128_bits_by_construction(self):
        assert qr_tokens.TOKEN_BYTES * 8 >= 128
        token = qr_tokens.new_token()
        assert HEX32.match(token)                            # 32 hex characters = 16 bytes = 128 bits, no padding
        assert len(bytes.fromhex(token)) * 8 >= 128

    def test_the_bits_come_straight_from_secrets_token_bytes(self, monkeypatch):
        calls = []
        real = qr_tokens.secrets.token_bytes

        def spy(n=None):
            calls.append(n)
            return real(n)

        monkeypatch.setattr(qr_tokens.secrets, "token_bytes", spy)
        token = qr_tokens.new_token()
        assert calls and all(n is not None and n * 8 >= 128 for n in calls)
        # a fixed source gives a fixed token: the token is a pure encoding of what secrets returned, nothing mixed in
        monkeypatch.setattr(qr_tokens.secrets, "token_bytes", lambda n=None: bytes(range(16)))
        assert qr_tokens.new_token() == bytes(range(16)).hex().upper()
        assert token != qr_tokens.new_token()

    def test_no_predictable_source_is_used_in_the_module(self):
        tree = ast.parse(pathlib.Path(qr_tokens.__file__).read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert "secrets" in imported
        assert not imported & {"random", "uuid", "time", "hashlib", "datetime"}, imported   # nothing derived from a clock, a counter or a name

    def test_five_thousand_tokens_are_unique_and_every_bit_position_is_balanced(self):
        n = 5000
        tokens = [qr_tokens.new_token() for _ in range(n)]
        assert len(set(tokens)) == n
        assert all(HEX32.match(t) for t in tokens)
        ones = [0] * 128
        for t in tokens:
            value = int(t, 16)
            for bit in range(128):
                ones[bit] += (value >> bit) & 1
        sigma = (n * 0.25) ** 0.5           # standard deviation of a fair coin flipped n times
        worst = max(abs(c - n / 2) for c in ones)
        assert worst < 6 * sigma, f"a bit position is biased: {worst / sigma:.1f} sigma from fair"   # 6 sigma: ~1e-9 false alarm rate
        # no run of tokens is sequential or repeating (a counter or timestamp would show up here)
        assert statistics.mean(int(t, 16) for t in tokens) / 2**128 == pytest.approx(0.5, abs=0.02)

    def test_a_token_does_not_depend_on_who_the_student_is(self, engine):
        a, b = new_student(engine, name="Same Name"), new_student(engine, name="Same Name")
        qr_tokens.generate_missing_tokens(engine)
        ta, tb = active_token(engine, a), active_token(engine, b)
        assert ta != tb
        for t, s in ((ta, a), (tb, b)):
            assert s.prn not in t and s.name.replace(" ", "").upper() not in t


# ═══════════════════════════════════════════════════════════════════════════════ THE QR IMAGE
class TestQrImageHoldsOnlyTheToken:
    def test_decoding_a_generated_qr_image_yields_only_the_token(self, engine):
        s = new_student(engine, name="Priya Sharma-Iyer", programme="M.Sc. Physics")
        qr_tokens.generate_missing_tokens(engine)
        token = active_token(engine, s)

        png = passes.qr_png(token)
        found = decode_qr(Image.open(io.BytesIO(png)))

        assert len(found) == 1                                   # one code, not several
        assert found[0].text == token                            # exactly the token...
        assert found[0].bytes == token.encode("ascii")           # ...as raw bytes too: no prefix, suffix, URL, JSON or newline
        assert len(found[0].text) == 32 and HEX32.match(found[0].text)
        for personal in (s.prn, s.name, "Priya", "Sharma", "Physics", str(s.id), "Q80"):
            assert personal.lower() not in found[0].text.lower()
        assert found[0].content_type == zxingcpp.ContentType.Text

    def test_the_qr_standard_symbol_is_version_3_level_q(self):
        found = decode_qr(Image.open(io.BytesIO(passes.qr_png(qr_tokens.new_token()))))[0]
        assert found.ec_level == "Q"                             # survives about a quarter of the symbol being damaged
        import segno
        code = segno.make(qr_tokens.new_token(), error="q", boost_error=False)
        assert code.version == 3 and code.symbol_size(scale=1, border=0)[0] == MODULES


# ═══════════════════════════════════════════════════════════════════════════════ REISSUE
def reissue(apps, student, reason="Student lost the pass", venue="college", client=None):
    body = {} if reason is None else {"reason": reason}
    return (client or admin(apps, venue)).post(f"/admin/api/students/{student.id}/reissue-qr", json=body)


class TestReissue:
    @pytest.fixture()
    def issued(self, engine):
        s = new_student(engine)
        qr_tokens.generate_missing_tokens(engine)
        s.token = active_token(engine, s)
        return s

    def test_reissue_deactivates_the_old_token_and_creates_a_new_one(self, apps, engine, world, issued):
        old = issued.token
        r = reissue(apps, issued, "Pass was soaked in the rain")
        assert r.status_code == 200, r.text

        tokens = token_rows(engine, issued)
        assert len(tokens) == 2                                                   # history is kept, nothing deleted
        first, second = tokens
        assert first["token"] == old and first["active"] is False                # the old token is NOT ACTIVE...
        assert first["deactivated_at"] is not None and first["deactivated_by"] == world.admin_id   # ...with who and when
        assert second["active"] is True and second["token"] != old               # a new one, active
        assert HEX32.match(second["token"])
        assert second["deactivated_at"] is None and second["deactivated_by"] is None
        assert scalar(engine, "SELECT count(*) FROM qr_tokens WHERE student_id = :s AND active", s=issued.id) == 1
        assert r.json()["new_token_id"] == second["id"] and r.json()["old_token_id"] == first["id"]
        assert old not in r.text and second["token"] not in r.text               # the secret is not echoed in the response

    def test_a_reason_is_mandatory_and_a_refusal_changes_nothing(self, apps, engine, issued):
        table, audit = token_table_fingerprint(engine), audit_count(engine)
        for reason in (None, "", "   ", "\n\t"):
            r = reissue(apps, issued, reason)
            assert r.status_code == 400 and error_code(r) == "REASON_REQUIRED", (reason, r.text)
        too_long = reissue(apps, issued, "x" * 501)
        assert too_long.status_code == 400 and error_code(too_long) == "REASON_TOO_LONG"
        assert (token_table_fingerprint(engine), audit_count(engine)) == (table, audit)   # no token moved, nothing logged

    def test_reissue_is_written_to_the_audit_log_with_the_reason_and_no_token_values(self, apps, engine, world, issued):
        old = issued.token
        r = reissue(apps, issued, "  Student   lost   the pass  ")
        entries = rows(engine, "SELECT * FROM audit_log WHERE action = 'QR_REISSUED' AND student_id = :s", s=issued.id)
        assert len(entries) == 1
        e = entries[0]
        assert e["operator_id"] == world.admin_id
        assert e["reason"] == "Student lost the pass"                              # whitespace tidied, wording kept
        assert e["venue_id"] == "college"
        assert e["details"]["old_token_id"] == r.json()["old_token_id"] and e["details"]["new_token_id"] == r.json()["new_token_id"]
        assert old not in str(e) and active_token(engine, issued) not in str(e)    # ids, never the secrets, in an exportable log

    def test_the_old_token_can_no_longer_be_used_and_the_new_one_can(self, apps, engine, world, issued):
        old = issued.token
        with engine.connect() as c:
            student, refused = pipeline.identify_by_token(c, old)
        assert refused is None and student["prn"] == issued.prn              # before: the token works

        assert reissue(apps, issued).status_code == 200
        new = active_token(engine, issued)

        with engine.connect() as c:
            student, refused = pipeline.identify_by_token(c, old)
            assert refused is not None and refused.result == "INVALID"       # token layer: refused
            assert refused.message == messages.REPLACED_QR
            student2, refused2 = pipeline.identify_by_token(c, new)
        assert refused2 is None and student2["id"] == issued.id              # the new token identifies the same student

    def test_and_at_a_real_station_too(self, apps, engine, world, issued):
        old = issued.token
        client = operator(apps, world, "REGISTRATION")
        assert reissue(apps, issued).status_code == 200
        new = active_token(engine, issued)
        refused = scan(client, "REGISTRATION", old)
        assert refused.json()["result"] == "INVALID" and refused.json()["message"] == messages.REPLACED_QR
        assert scan(client, "REGISTRATION", new).json()["result"] == "READY"

    def test_reissuing_again_leaves_only_the_latest_active(self, apps, engine, issued):
        t1 = issued.token
        assert reissue(apps, issued, "first").status_code == 200
        t2 = active_token(engine, issued)
        assert reissue(apps, issued, "second").status_code == 200
        t3 = active_token(engine, issued)
        assert len({t1, t2, t3}) == 3
        assert [(r["token"], r["active"]) for r in token_rows(engine, issued)] == [(t1, False), (t2, False), (t3, True)]

    def test_two_simultaneous_reissues_still_leave_exactly_one_active_token(self, engine, world, issued):
        results = _run_threads(lambda i: qr_tokens.reissue_token(engine, student_id=issued.id, reason=f"both admins clicked {i}",
                                                                 operator_id=world.admin_id, venue_id="college"), 2)
        assert not [r for r in results if isinstance(r, Exception)], results
        tokens = token_rows(engine, issued)
        assert len(tokens) == 3 and [t["active"] for t in tokens].count(True) == 1     # one active, two in history, none lost

    def test_reissue_is_atomic_if_the_audit_write_fails_nothing_changes(self, engine, world, issued, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("audit store unavailable")

        monkeypatch.setattr(qr_tokens, "write_audit", boom)
        before = token_table_fingerprint(engine)
        with pytest.raises(RuntimeError):
            qr_tokens.reissue_token(engine, student_id=issued.id, reason="test", operator_id=world.admin_id, venue_id="college")
        assert token_table_fingerprint(engine) == before                         # the old token is still the one active token
        assert active_token(engine, issued) == issued.token

    def test_reissue_touches_no_activity_history(self, apps, engine, issued):
        events = scalar(engine, "SELECT count(*) FROM activity_events")
        outbox = scalar(engine, "SELECT count(*) FROM outbox")
        assert reissue(apps, issued).status_code == 200
        assert (scalar(engine, "SELECT count(*) FROM activity_events"), scalar(engine, "SELECT count(*) FROM outbox")) == (events, outbox)

    def test_only_the_admin_can_reissue(self, apps, engine, world, issued):
        before = (token_table_fingerprint(engine), audit_count(engine))
        for activity in ACTIVITIES:
            assert reissue(apps, issued, client=operator(apps, world, activity)).status_code == 403, activity
        assert reissue(apps, issued, client=TestClient(apps["college"])).status_code == 401
        assert (token_table_fingerprint(engine), audit_count(engine)) == before

    def test_the_central_server_can_reissue_too(self, apps, engine, issued):
        assert reissue(apps, issued, venue="central").status_code == 200
        assert rows(engine, "SELECT venue_id FROM audit_log WHERE action = 'QR_REISSUED' AND student_id = :s", s=issued.id)[0]["venue_id"] is None

    def test_unknown_and_malformed_students_get_a_plain_404_and_inactive_ones_a_409(self, apps, engine):
        client = admin(apps, "college")
        for bad in (str(uuid.uuid4()), "not-a-uuid", "1' OR '1'='1"):
            r = client.post(f"/admin/api/students/{bad}/reissue-qr", json={"reason": "x"})
            assert r.status_code == 404 and error_code(r) == "STUDENT_NOT_FOUND", (bad, r.text)
        inactive = make_student(engine, active=False)
        r = reissue(apps, inactive)
        assert r.status_code == 409 and error_code(r) == "STUDENT_NOT_ACTIVE"
        assert len(token_rows(engine, inactive)) == 1 and token_rows(engine, inactive)[0]["active"]   # untouched

    def test_a_student_with_no_token_yet_is_simply_issued_one(self, apps, engine):
        s = new_student(engine)
        r = reissue(apps, s, "Issue by hand")
        assert r.status_code == 200 and r.json()["old_token_id"] is None
        assert len(token_rows(engine, s)) == 1 and token_rows(engine, s)[0]["active"]

    def test_the_admin_page_offers_reissue_and_the_form_works(self, apps, engine, issued):
        client = admin(apps, "college")
        page = client.get(f"/admin/students/{issued.id}")
        assert page.status_code == 200
        assert f"/admin/students/{issued.id}/reissue-qr" in page.text and "Reissue QR" in page.text
        assert f"/admin/api/students/{issued.id}/pass.pdf" in page.text or f"/admin/students/{issued.id}/pass.pdf" in page.text
        assert issued.token not in page.text                                     # the secret is never shown on a page
        no_reason = client.post(f"/admin/students/{issued.id}/reissue-qr", data={"reason": ""}, follow_redirects=False)
        assert no_reason.status_code == 303 and len(token_rows(engine, issued)) == 1
        done = client.post(f"/admin/students/{issued.id}/reissue-qr", data={"reason": "smudged"}, follow_redirects=False)
        assert done.status_code == 303 and len(token_rows(engine, issued)) == 2


# ═══════════════════════════════════════════════════════════════════════════════ THE PASS
class TestPassPdf:
    def test_the_pass_holds_name_prn_programme_photo_event_and_the_keep_line(self, engine, tmp_path):
        s = make_pass(engine, tmp_path, name="Ananya Krishnamurthy", programme="Master of Business Administration")
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        pdf = result.pdf

        assert pdf.startswith(b"%PDF") and page_count(pdf) == 1
        shown = cell_text(pdf, 0, 0, 0)
        assert "Ananya Krishnamurthy" in shown
        assert s.prn in shown
        assert "Master of Business Administration" in shown
        assert EVENT in shown
        assert re.search(r"keep .*whole event", shown, re.I), shown             # the instruction line
        assert "NO PHOTO" not in shown

        images = embedded_images(pdf)
        assert len(images) == 1                                                  # the photo, embedded once
        assert max(images[0].image.size) <= 600                                  # downscaled: a 600x800 source is not shipped as-is
        assert result.warnings == []

        # the photo is really painted on the page (a solid red photo -> a large red area on the rendered pass)
        page = render_page(pdf)
        rgb = np.asarray(page)
        red = int(((rgb[..., 0] > 150) & (rgb[..., 1] < 90) & (rgb[..., 2] < 90)).sum())
        assert red > 0.5 * (30 * 38) * (300 / 25.4) ** 2                         # more than half of a 30x38 mm block at 300 dpi

    def test_the_qr_on_the_pass_decodes_to_that_students_active_token_and_nothing_else(self, engine, tmp_path):
        s = make_pass(engine, tmp_path)
        pdf = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf
        found = decode_qr(render_page(pdf))
        assert len(found) == 1
        assert found[0].text == s.token == active_token(engine, s)
        assert found[0].bytes == s.token.encode("ascii")
        assert s.prn not in found[0].text and s.name not in found[0].text

    def test_the_qr_also_decodes_from_a_poor_200_dpi_print(self, engine, tmp_path):
        s = make_pass(engine, tmp_path)
        pdf = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf
        assert [r.text for r in decode_qr(render_page(pdf, dpi=200))] == [s.token]
        assert [r.text for r in decode_qr(render_page(pdf, dpi=150))] == [s.token]

    def test_the_qr_is_drawn_at_the_stated_physical_size_with_a_full_quiet_zone(self, engine, tmp_path):
        s = make_pass(engine, tmp_path)
        pdf = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf
        dpi = 600
        page = render_page(pdf, dpi=dpi).convert("L")
        px_per_mm = dpi / 25.4
        symbol = decode_qr(page)[0].position                                     # the code's own corners, from the decoder
        xs = [p.x for p in (symbol.top_left, symbol.top_right, symbol.bottom_left, symbol.bottom_right)]
        ys = [p.y for p in (symbol.top_left, symbol.top_right, symbol.bottom_left, symbol.bottom_right)]

        # find the symbol's ink bounding box near the decoder's corners
        pad = int(2 * MODULE_MM * px_per_mm)
        region = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
        ink = page.crop(region).point(lambda v: 255 if v < 128 else 0).getbbox()
        width_mm = (ink[2] - ink[0]) / px_per_mm
        module_mm = width_mm / MODULES
        assert width_mm == pytest.approx(MODULES * MODULE_MM, abs=0.4), width_mm  # 36.8 mm square
        assert module_mm >= 1.0                                                   # >= 1 mm per module: far above any scanner's minimum
        assert 35 <= width_mm <= 45

        # quiet zone: nothing but paper within 4 modules of the symbol, on every side
        left, top = region[0] + ink[0], region[1] + ink[1]
        right, bottom = region[0] + ink[2], region[1] + ink[3]
        quiet = int(QUIET_MODULES * module_mm * px_per_mm)
        around = page.crop((left - quiet, top - quiet, right + quiet, bottom + quiet)).point(lambda v: 255 if v < 200 else 0)
        around.paste(0, (quiet, quiet, quiet + (right - left), quiet + (bottom - top)))   # blank out the symbol itself
        assert around.getbbox() is None, "ink inside the QR quiet zone"
        # the symbol sits fully inside the pass with a printer-safe margin (office printers cannot print to the paper edge)
        assert left / px_per_mm > 8 and (page.width - right) / px_per_mm > 8

    def test_the_pass_is_the_stated_size(self, engine, tmp_path):
        s = make_pass(engine, tmp_path)
        w, h = page_size_mm(passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf)
        assert (round(w, 1), round(h, 1)) == (CELL_W_MM, CELL_H_MM)

    def test_a_missing_photo_gives_a_clearly_marked_placeholder_and_a_warning(self, engine, tmp_path):
        s = make_pass(engine, tmp_path, photo=None)
        result = passes.render_single(pass_data(s, s.token, None), EVENT)
        assert "NO PHOTO" in cell_text(result.pdf, 0, 0, 0)
        assert embedded_images(result.pdf) == []
        assert [w.code for w in result.warnings] == ["NO_PHOTO"] and result.warnings[0].prn == s.prn
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]   # the pass still works

    @pytest.mark.parametrize("kind", ["path_does_not_exist", "not_an_image", "empty_file", "directory"])
    def test_an_unusable_photo_never_stops_the_pass(self, engine, tmp_path, kind):
        s = make_pass(engine, tmp_path, photo=None)
        bad = tmp_path / f"{kind}.jpg"
        if kind == "not_an_image":
            bad.write_bytes(b"this is not a picture")
        elif kind == "empty_file":
            bad.write_bytes(b"")
        elif kind == "directory":
            bad.mkdir()
        result = passes.render_single(pass_data(s, s.token, str(bad)), EVENT)
        assert "NO PHOTO" in cell_text(result.pdf, 0, 0, 0)
        assert [w.code for w in result.warnings] == ["NO_PHOTO" if kind == "path_does_not_exist" else "PHOTO_UNREADABLE"]
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]

    @pytest.mark.parametrize("size,mode,fmt", [((4000, 3000), "RGB", "JPEG"), ((300, 900), "RGB", "PNG"), ((500, 500), "RGBA", "PNG"), ((80, 100), "L", "PNG")])
    def test_photos_of_any_shape_are_fitted_and_shrunk(self, engine, tmp_path, size, mode, fmt):
        s = make_pass(engine, tmp_path, photo=None)
        path = write_photo(tmp_path, f"{s.prn}-x.png", size=size, mode=mode, fmt=fmt, color=(30, 60, 200) if mode != "L" else 90)
        result = passes.render_single(pass_data(s, s.token, path), EVENT)
        images = embedded_images(result.pdf)
        assert len(images) == 1 and max(images[0].image.size) <= 600
        assert result.warnings == []
        assert len(result.pdf) < 250_000                                         # a 4000x3000 photo does not make a 12 MB pass

    def test_a_very_long_name_is_shrunk_to_fit_and_printed_in_full(self, engine, tmp_path):
        name = "Venkata Subrahmanyam Lakshmi Narasimha Rajeswara Prasad Chandrasekhara Bhattacharyya-Vishwanathan of Vijayawada"
        s = make_pass(engine, tmp_path, name=name)
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert shows(cell_text(result.pdf, 0, 0, 0), name)
        assert result.warnings == []
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]  # the name never collides with the QR

    def test_a_name_at_the_importers_200_character_limit_still_prints_in_full(self, engine, tmp_path):
        name = " ".join(["Venkata Subrahmanyam Lakshmi Narasimha Chandrasekhara"] * 4)[:200].rstrip()
        assert len(name) >= 195
        s = make_pass(engine, tmp_path, name=name)
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert result.warnings == []
        assert shows(cell_text(result.pdf, 0, 0, 0), name)
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]

    def test_an_absurd_name_is_cut_with_dots_and_flagged_never_silently(self, engine, tmp_path):
        name = " ".join(["Subrahmanyeswara"] * 12)[:200]           # 200 characters of 16-letter words: cannot fit even at 7 pt
        s = make_pass(engine, tmp_path, name=name)
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert [w.code for w in result.warnings] == ["NAME_TRUNCATED"] and result.warnings[0].prn == s.prn
        shown = cell_text(result.pdf, 0, 0, 0)
        assert "..." in shown and name not in shown
        assert s.prn in shown and [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]   # the pass is still usable

    def test_a_very_long_programme_is_kept_and_does_not_reach_the_qr(self, engine, tmp_path):
        programme = "Master of Science in Advanced Computational Techniques for Interdisciplinary Data-Intensive Environmental Research"
        s = make_pass(engine, tmp_path, programme=programme)
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert shows(cell_text(result.pdf, 0, 0, 0), programme)
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]

    @pytest.mark.parametrize("name", ["José Müller", "Siobhán O'Brien", "Zoë Ångström-Łukasiewicz"])
    def test_accented_latin_names_print_exactly(self, engine, tmp_path, name):
        s = make_pass(engine, tmp_path, name=name)
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert shows(cell_text(result.pdf, 0, 0, 0), name)
        assert result.warnings == []

    def test_a_word_is_never_broken_in_the_middle_a_smaller_size_or_a_hyphen_break_is_used(self, engine, tmp_path):
        s = make_pass(engine, tmp_path, name="Zoë Ångström-Łukasiewicz")
        pdf = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf
        lines = [ln.strip() for ln in pdfium.PdfDocument(pdf)[0].get_textpage().get_text_range().splitlines()]
        assert any("Łukasiewicz" in ln for ln in lines), lines                          # the surname is on one line, whole
        assert not [ln for ln in lines if "Łukas" in ln and "Łukasiewicz" not in ln], lines
        assert not [ln for ln in lines if ln.startswith("iewicz")], lines               # no "Łukas" / "iewicz" split

    def test_a_name_in_a_script_the_font_cannot_draw_is_flagged_not_printed_as_boxes(self, engine, tmp_path):
        s = make_pass(engine, tmp_path, name="अनिल कुमार")
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        assert "NAME_CHARACTERS" in [w.code for w in result.warnings]
        shown = cell_text(result.pdf, 0, 0, 0)
        assert "अ" not in shown and s.prn in shown                               # no garbage glyphs; PRN and QR still right
        assert [r.text for r in decode_qr(render_page(result.pdf))] == [s.token]

    def test_special_characters_in_text_fields_cannot_break_the_pass(self, engine, tmp_path):
        s = make_pass(engine, tmp_path, name="<b>Bold</b> & \"Quotes\" (100%) \\ /", programme="B.Sc. <i>Physics</i> & Maths")
        result = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT)
        shown = cell_text(result.pdf, 0, 0, 0)
        assert "<b>Bold</b> & \"Quotes\" (100%)" in shown and "<i>Physics</i>" in shown   # printed literally, not interpreted as markup

    def test_the_token_is_not_printed_as_text_on_the_pass(self, engine, tmp_path):
        s = make_pass(engine, tmp_path)
        pdf = passes.render_single(pass_data(s, s.token, s.photo_path), EVENT).pdf
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        assert s.token not in reader.pages[0].extract_text() and s.token.encode() not in pdf   # only the QR carries it


# ═══════════════════════════════════════════════════════════════════════════════ DOWNLOADS
class TestDownloads:
    @pytest.fixture()
    def batch(self, engine, tmp_path):
        """9 active students of two schools inserted in SCRAMBLED sequence order, one without a photo, plus one
        inactive student. Returns the active ones in correct (sequence) order."""
        run = next(_N)
        school = f"School of Testing {run}"
        base = run * 100                                                                  # sequence numbers are unique across the module
        sequences = [base + k for k in (4, 1, 8, 0, 6, 2, 7, 3, 5)]                       # deliberately not in order
        # names run Z..R against sequence 0..8, so alphabetical order is the REVERSE of ceremony order: sorting by
        # name, PRN or insertion order instead of sequence_no would put the passes in a visibly different order
        students = [new_student(engine, school=school, sequence_no=seq, name=f"{'ZYXWVUTSR'[seq - base]} Batch Student {seq}",
                                photo_path=None if seq == base + 6 else write_photo(tmp_path, f"b{seq}.jpg", color=(20, 60 + (seq % 100), 20 + (seq % 200))))
                    for seq in sequences]
        inactive = new_student(engine, school=school, active=False, sequence_no=base + 99)
        qr_tokens.generate_missing_tokens(engine)
        for s in students:
            s.token = active_token(engine, s)
            s.seq = scalar(engine, "SELECT sequence_no FROM students WHERE id = :i", i=s.id)
        ordered = sorted(students, key=lambda s: s.seq)
        return SimpleNamespace(school=school, ordered=ordered, all=students, inactive=inactive)

    def test_individual_download_is_a_pdf_of_that_students_pass(self, apps, engine, batch):
        s = batch.ordered[3]
        r = admin(apps, "college").get(f"/admin/api/students/{s.id}/pass.pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert f"pass-{s.prn}.pdf" in r.headers["content-disposition"] and "attachment" in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store"
        assert [x.text for x in decode_qr(render_page(r.content))] == [s.token]
        assert s.prn in cell_text(r.content, 0, 0, 0) and s.name in cell_text(r.content, 0, 0, 0)

    def test_bulk_download_for_n_students_has_n_passes_in_sequence_order(self, apps, engine, batch):
        r = admin(apps, "college").get("/admin/api/passes.pdf", params={"school": batch.school})
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        pdf, expected = r.content, batch.ordered
        assert len(expected) == 9 and page_count(pdf) == 3                                # 4 + 4 + 1 on A4 sheets
        assert all(round(x, 0) in (210, 297) for x in page_size_mm(pdf))

        seen = []
        for index in range(page_count(pdf)):
            image = render_page(pdf, index, dpi=300)
            for row in range(2):
                for col in range(2):
                    n = index * 4 + row * 2 + col
                    cell = image.crop(cell_box_px(row, col, 300))
                    found = decode_qr(cell)
                    if n >= len(expected):
                        assert found == [] and cell_text(pdf, index, row, col) == "", f"an extra pass at page {index} r{row} c{col}"
                        continue
                    who = expected[n]
                    # the QR printed in THIS cell is the token of the student whose PRN, name and programme are printed in this cell
                    assert [x.text for x in found] == [who.token], f"pass {n}: wrong QR"
                    shown = cell_text(pdf, index, row, col)
                    assert who.prn in shown and who.name in shown and who.programme in shown, f"pass {n}: {shown!r}"
                    seen.append(who.prn)
        assert seen == [s.prn for s in batch.ordered]                                     # every pass exactly once, in order

    def test_the_inactive_student_gets_no_pass_and_a_missing_photo_gets_a_placeholder(self, apps, batch):
        r = admin(apps, "college").get("/admin/api/passes.pdf", params={"school": batch.school})
        text_all = " ".join(cell_text(r.content, p, rr, cc) for p in range(page_count(r.content)) for rr in range(2) for cc in range(2))
        assert batch.inactive.prn not in text_all and batch.inactive.name not in text_all
        assert text_all.count("NO PHOTO") == 1                                             # the one student with no photo file
        assert int(r.headers["x-pass-warnings"]) == 1
        assert sum(len(embedded_images(r.content, p)) for p in range(page_count(r.content))) == 8

    def test_a_sheet_keeps_dark_ink_well_inside_every_pass_and_marks_where_to_cut(self, apps, batch):
        r = admin(apps, "college").get("/admin/api/passes.pdf", params={"school": batch.school})
        image = render_page(r.content, 0, dpi=300).convert("L")
        px = 300 / 25.4
        for row in range(2):
            for col in range(2):
                cell = image.crop(cell_box_px(row, col, 300))
                dark = np.asarray(cell) < 128
                ys, xs = np.nonzero(dark)
                # office printers cannot print to the paper edge (typically 4-5 mm): keep all dark ink >= 7 mm inside the cell
                assert xs.min() / px >= 7 and ys.min() / px >= 7, (row, col)
                assert (cell.width - xs.max()) / px >= 7 and (cell.height - ys.max()) / px >= 7, (row, col)
        # ...and light cut lines run down and across the middle of the sheet
        arr = np.asarray(image)
        vertical = arr[int(20 * px):int(60 * px), int(CELL_W_MM * px) - 2:int(CELL_W_MM * px) + 3]
        horizontal = arr[int(CELL_H_MM * px) - 2:int(CELL_H_MM * px) + 3, int(20 * px):int(60 * px)]
        assert vertical.min() < 250 and horizontal.min() < 250 and vertical.min() >= 128

    def test_offset_and_limit_split_a_big_batch_into_files(self, apps, batch):
        client = admin(apps, "college")
        first = client.get("/admin/api/passes.pdf", params={"school": batch.school, "limit": 4})
        rest = client.get("/admin/api/passes.pdf", params={"school": batch.school, "offset": 4})
        assert page_count(first.content) == 1 and page_count(rest.content) == 2
        tokens = [x.text for r in (first, rest) for p in range(page_count(r.content)) for x in decode_qr(render_page(r.content, p))]
        assert tokens == [s.token for s in batch.ordered]                                # the split is lossless and in order

    def test_the_whole_batch_download_contains_every_active_student_once(self, apps, engine, batch):
        r = admin(apps, "college").get("/admin/api/passes.pdf")
        active = scalar(engine, "SELECT count(*) FROM students WHERE status = 'ACTIVE'")
        assert r.status_code == 200 and page_count(r.content) == -(-active // 4)
        got = [x.text for p in range(page_count(r.content)) for x in decode_qr(render_page(r.content, p, dpi=150))]
        assert sorted(got) == sorted(t for (t,) in [(x["token"],) for x in rows(engine, "SELECT token FROM qr_tokens WHERE active AND student_id IN (SELECT id FROM students WHERE status = 'ACTIVE')")])

    def test_a_student_without_a_token_stops_the_download_instead_of_shortening_the_sheet(self, apps, engine, batch):
        missing = new_student(engine, school=batch.school)
        client = admin(apps, "college")
        r = client.get("/admin/api/passes.pdf", params={"school": batch.school})
        assert r.status_code == 409 and error_code(r) == "TOKENS_MISSING"
        one = client.get(f"/admin/api/students/{missing.id}/pass.pdf")
        assert one.status_code == 409 and error_code(one) == "NO_TOKEN"
        qr_tokens.generate_missing_tokens(engine)
        assert client.get("/admin/api/passes.pdf", params={"school": batch.school}).status_code == 200

    def test_no_pass_for_an_inactive_student_an_unknown_student_or_an_empty_school(self, apps, engine, batch):
        client = admin(apps, "college")
        inactive = make_student(engine, active=False)
        r = client.get(f"/admin/api/students/{inactive.id}/pass.pdf")
        assert r.status_code == 409 and error_code(r) == "STUDENT_NOT_ACTIVE"
        assert client.get(f"/admin/api/students/{uuid.uuid4()}/pass.pdf").status_code == 404
        assert client.get("/admin/api/students/not-a-uuid/pass.pdf").status_code == 404
        empty = client.get("/admin/api/passes.pdf", params={"school": "No Such School"})
        assert empty.status_code == 404 and error_code(empty) == "NO_STUDENTS"
        assert client.get("/admin/api/passes.pdf", params={"limit": 0}).status_code == 400
        assert client.get("/admin/api/passes.pdf", params={"offset": -1}).status_code == 400
        for huge in ("99999999999999999999", "abc", "1e3"):
            assert client.get("/admin/api/passes.pdf", params={"offset": huge}).status_code == 400, huge

    def test_downloads_are_admin_only_because_a_pass_carries_a_live_token(self, apps, world, batch):
        s = batch.ordered[0]
        for url in (f"/admin/api/students/{s.id}/pass.pdf", "/admin/api/passes.pdf"):
            for activity in ("REGISTRATION", "LUNCH"):
                assert operator(apps, world, activity).get(url).status_code == 403, (url, activity)
            assert TestClient(apps["college"]).get(url).status_code == 401

    def test_downloads_are_logged_without_any_token(self, apps, engine, batch):
        client = admin(apps, "college")
        s = batch.ordered[1]
        one_before, bulk_before = audit_count(engine, "PASS_DOWNLOADED"), audit_count(engine, "PASSES_DOWNLOADED")
        client.get(f"/admin/api/students/{s.id}/pass.pdf")
        client.get("/admin/api/passes.pdf", params={"school": batch.school})
        assert audit_count(engine, "PASS_DOWNLOADED") == one_before + 1 and audit_count(engine, "PASSES_DOWNLOADED") == bulk_before + 1
        one = rows(engine, "SELECT * FROM audit_log WHERE action = 'PASS_DOWNLOADED' ORDER BY id DESC LIMIT 1")[0]
        bulk = rows(engine, "SELECT * FROM audit_log WHERE action = 'PASSES_DOWNLOADED' ORDER BY id DESC LIMIT 1")[0]
        assert one["student_id"] == s.id and bulk["details"]["count"] == 9 and bulk["details"]["school"] == batch.school
        assert not [t for t in (x.token for x in batch.all) if t in str(one) or t in str(bulk)]

    def test_a_download_changes_no_token(self, apps, engine, batch):
        before = token_table_fingerprint(engine)
        admin(apps, "college").get("/admin/api/passes.pdf", params={"school": batch.school})
        admin(apps, "college").get(f"/admin/api/students/{batch.ordered[0].id}/pass.pdf")
        assert token_table_fingerprint(engine) == before

    def test_the_admin_screen_has_the_generate_and_bulk_download_controls(self, apps):
        page = admin(apps, "college").get("/admin/passes")
        assert page.status_code == 200
        assert "/admin/qr/generate-missing" in page.text and "/admin/api/passes.pdf" in page.text
        assert "actual size" in page.text.lower()                                        # the print instruction (100%, not fit-to-page)

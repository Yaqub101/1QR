"""The university's real data has no Convocation Sequence Number and no Seat Number column.

WHAT IS UNDER TEST
  * `students.sequence_no` is nullable and NOT unique: the column survives for the day the
    university supplies numbers, but nothing may require, assume or collide on it.
  * `students.seat_no` is nullable and unused: Seating is a plain "this student is seated"
    confirmation, exactly like Robe Allocation.
  * No code path orders by, displays or validates against either column. The order the ceremony
    actually runs on is the queue confirmation order, which was already built.
  * The printable pass prints a sequence line only when there is a number to print.

The expectations below are written out by hand from the change request, NOT imported from the
code they check.
"""
import io
import pathlib
import re
import uuid

import pytest
from sqlalchemy import text

from backend import passes
from backend.engine import extensions
from backend.engine.activities import ACTIVITY_CONFIGS
from backend.engine.model import DUPLICATE_PLACEHOLDERS, RECORDABLE_STUDENT_FIELDS
from backend.importer import commit_import, detect_column_mapping, parse_file, validate_import
from backend.stage import state as stage_state
from tests.admin_support import rows, scalar
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    confirm,
    engine,
    field,
    make_student,
    operator,
    scan,
    seed_events,
    world,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

# Written out by hand from the change request: what each station's card may show now.
DISPLAY_KEYS_AFTER = {
    "REGISTRATION": ["prn", "programme", "school"],
    "THOBE_ALLOCATION": ["prn", "programme", "school"],
    "SEATING": ["prn", "programme", "school"],
    "QUEUE": ["prn", "queue_position"],
    "STAGE": ["programme", "school"],
    "THOBE_RETURN": ["prn", "thobe_issued"],
    "LUNCH": ["prn", "eligibility"],
}


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


def insert_student(engine, *, prn=None, sequence_no=None, seat_no=None, name=None):
    prn = prn or f"NOSEQ-{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        sid = c.execute(
            text("INSERT INTO students (prn, name, programme, school, sequence_no, seat_no) "
                 "VALUES (:p, :n, 'B.Tech Computer Science', 'School of Engineering', :seq, :seat) RETURNING id"),
            {"p": prn, "n": name or f"Student {prn}", "seq": sequence_no, "seat": seat_no}).scalar_one()
    return sid, prn


# ===================================================================== THE SCHEMA
class TestSchema:
    def test_a_student_can_be_stored_with_no_sequence_number_and_no_seat(self, engine):
        sid, _ = insert_student(engine)
        assert rows(engine, "SELECT sequence_no, seat_no FROM students WHERE id = :i", i=sid)[0] == {
            "sequence_no": None, "seat_no": None}

    def test_two_students_may_share_a_sequence_number(self, engine):
        # The unique constraint is gone: if the university ever sends numbers, a clash in their
        # file must not stop the import on event eve.
        insert_student(engine, sequence_no=424_242)
        insert_student(engine, sequence_no=424_242)
        assert scalar(engine, "SELECT count(*) FROM students WHERE sequence_no = 424242") == 2

    def test_any_number_of_students_may_have_no_sequence_number(self, engine):
        before = scalar(engine, "SELECT count(*) FROM students WHERE sequence_no IS NULL")
        for _ in range(3):
            insert_student(engine)
        assert scalar(engine, "SELECT count(*) FROM students WHERE sequence_no IS NULL") == before + 3

    def test_the_unique_index_on_sequence_no_no_longer_exists(self, engine):
        names = {r["indexname"] for r in rows(engine, "SELECT indexname FROM pg_indexes WHERE tablename = 'students'")}
        assert "students_sequence_no_key" not in names, names
        assert "students_prn_key" in names, "the PRN must stay unique: it is the import key"

    def test_both_columns_are_nullable_in_the_catalogue(self, engine):
        info = rows(engine, "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_name = 'students' AND column_name IN ('sequence_no', 'seat_no')")
        assert {r["column_name"]: r["is_nullable"] for r in info} == {"sequence_no": "YES", "seat_no": "YES"}

    def test_a_sequence_number_that_is_given_must_still_be_a_positive_number(self, engine):
        with pytest.raises(Exception):
            insert_student(engine, sequence_no=0)


# ===================================================================== THE IMPORTER
class TestImportWithoutSequenceOrSeat:
    @staticmethod
    def _csv(prns):
        header = "PRN,Student Name,Programme,School\n"
        return (header + "".join(f"{p},Name {p},B.Tech,School of Engineering\n" for p in prns)).encode()

    def test_a_file_with_neither_column_imports_cleanly(self, engine):
        prns = [f"NOCOL-{uuid.uuid4().hex[:8]}" for _ in range(3)]
        cols, file_rows = parse_file(io.BytesIO(self._csv(prns)), "real.csv")
        mapping = detect_column_mapping(cols)
        assert "sequence_no" not in mapping.values() and "seat_no" not in mapping.values()
        with engine.connect() as conn:
            preview = validate_import(file_rows, mapping, conn)
            assert preview.is_valid is True and preview.errors == []
            assert len(preview.to_create) == 3
            assert commit_import(preview, conn).created == 3
        stored = rows(engine, "SELECT sequence_no, seat_no FROM students WHERE prn = ANY(:p)", p=prns)
        assert stored == [{"sequence_no": None, "seat_no": None}] * 3

    def test_a_missing_sequence_number_is_at_most_a_note_never_an_error(self, engine):
        rows_in = [{"PRN": f"W-{uuid.uuid4().hex[:8]}", "Name": "A", "Programme": "CS", "School": "Eng"}]
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school"}
        with engine.connect() as conn:
            preview = validate_import(rows_in, mapping, conn)
        assert preview.is_valid is True
        assert all(e.field != "sequence_no" for e in preview.errors)

    def test_duplicate_sequence_numbers_are_a_warning_and_still_import(self, engine):
        a, b = f"D-{uuid.uuid4().hex[:8]}", f"D-{uuid.uuid4().hex[:8]}"
        rows_in = [{"PRN": a, "Name": "A", "Programme": "CS", "School": "Eng", "Sequence No": 900001},
                   {"PRN": b, "Name": "B", "Programme": "CS", "School": "Eng", "Sequence No": 900001}]
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school",
                   "Sequence No": "sequence_no"}
        with engine.connect() as conn:
            preview = validate_import(rows_in, mapping, conn)
            assert preview.is_valid is True, [e.message for e in preview.errors]
            assert commit_import(preview, conn).created == 2
        assert scalar(engine, "SELECT count(*) FROM students WHERE sequence_no = 900001") == 2

    def test_a_number_that_is_supplied_is_still_kept(self, engine):
        prn = f"KEEP-{uuid.uuid4().hex[:8]}"
        rows_in = [{"PRN": prn, "Name": "A", "Programme": "CS", "School": "Eng", "Sequence No": "910007"}]
        mapping = {"PRN": "prn", "Name": "name", "Programme": "programme", "School": "school",
                   "Sequence No": "sequence_no"}
        with engine.connect() as conn:
            commit_import(validate_import(rows_in, mapping, conn), conn)
        assert scalar(engine, "SELECT sequence_no FROM students WHERE prn = :p", p=prn) == 910007


# ===================================================================== THE STATION SCREENS
class TestStationScreens:
    def test_no_activity_shows_a_sequence_number_or_a_seat(self):
        assert {a: list(cfg.display_fields) for a, cfg in ACTIVITY_CONFIGS.items()} == DISPLAY_KEYS_AFTER

    def test_the_engine_has_no_sequence_or_seat_building_blocks_left(self):
        assert "sequence_no" not in extensions.DISPLAY_FIELDS
        assert "seat_no" not in extensions.DISPLAY_FIELDS
        assert "sequence_no" not in RECORDABLE_STUDENT_FIELDS
        assert "seat_no" not in RECORDABLE_STUDENT_FIELDS
        assert "seat_no" not in DUPLICATE_PLACEHOLDERS

    def test_no_activity_records_a_seat_or_a_sequence_number_on_its_event(self):
        assert all(cfg.record_fields == () for cfg in ACTIVITY_CONFIGS.values())

    def test_seating_asks_for_the_same_things_as_thobe_allocation(self):
        seating, robe = ACTIVITY_CONFIGS["SEATING"], ACTIVITY_CONFIGS["THOBE_ALLOCATION"]
        assert seating.display_fields == robe.display_fields
        assert "{seat_no}" not in seating.duplicate_message

    def test_a_seated_student_is_confirmed_with_no_seat_anywhere_on_the_card_or_the_event(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION"])
        client = operator(apps, world, "SEATING")
        card = scan(client, "SEATING", s.token).json()
        assert card["result"] == "READY"
        keys = [f["key"] for f in card["student"]["fields"]]
        assert "seat_no" not in keys and "sequence_no" not in keys
        assert not any("seat" in f["label"].lower() for f in card["student"]["fields"])
        assert confirm(client, "SEATING", token=s.token).json()["result"] == "CONFIRMED"
        stored = rows(engine, "SELECT details FROM activity_events WHERE student_id = :s AND activity = 'SEATING'", s=s.id)
        assert stored[0]["details"] == {}

    def test_the_duplicate_message_for_seating_names_no_seat(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "SEATING"])
        body = scan(operator(apps, world, "SEATING"), "SEATING", s.token).json()
        assert body["result"] == "DUPLICATE"
        assert "SEAT " not in body["message"].replace("SEATING", "")

    def test_the_queue_card_shows_no_sequence_number(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "SEATING"])
        card = scan(operator(apps, world, "QUEUE"), "QUEUE", s.token).json()["student"]
        assert [f["key"] for f in card["fields"]] == ["prn", "queue_position"]
        assert "Position" in field(card, "queue_position")

    def test_a_student_with_neither_number_goes_through_every_stadium_station(self, apps, world, engine):
        s = make_student(engine)
        with engine.begin() as c:
            c.execute(text("UPDATE students SET sequence_no = NULL, seat_no = NULL WHERE id = :i"), {"i": s.id})
        for activity in ("REGISTRATION", "THOBE_ALLOCATION", "SEATING", "QUEUE", "STAGE"):
            body = confirm(operator(apps, world, activity), activity, token=s.token).json()
            assert body["result"] == "CONFIRMED", (activity, body)


# ===================================================================== ORDERING
class TestOrdering:
    def test_the_stage_and_engine_code_never_mentions_either_column(self):
        for folder in ("stage", "engine"):
            for path in sorted((BACKEND / folder).rglob("*.py")):
                source = path.read_text(encoding="utf-8")
                assert "sequence_no" not in source, path
                assert "seat_no" not in source, path

    def test_nothing_anywhere_orders_by_a_sequence_number_without_allowing_for_none(self):
        """A column that is usually NULL cannot decide an order on its own: every remaining ORDER BY
        that mentions it must say NULLS LAST and fall through to a key that is always there."""
        offenders = []
        for path in sorted(BACKEND.rglob("*.py")):
            for match in re.finditer(r"ORDER BY[^\"')]*sequence_no[^\"')]*", path.read_text(encoding="utf-8"), re.I):
                if "NULLS LAST" not in match.group(0).upper():
                    offenders.append(f"{path.name}: {match.group(0)}")
        assert offenders == [], offenders

    def test_the_stage_queue_is_ordered_purely_by_confirmation_order(self, apps, world, engine):
        made = []
        for _ in range(3):
            s = make_student(engine)
            with engine.begin() as c:
                c.execute(text("UPDATE students SET sequence_no = NULL WHERE id = :i"), {"i": s.id})
            seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "SEATING"])
            made.append(s)
        # Confirm the queue in the REVERSE of the order the students were created in.
        for s in reversed(made):
            assert confirm(operator(apps, world, "QUEUE"), "QUEUE", token=s.token).json()["result"] == "CONFIRMED"
        mine = {str(s.id) for s in made}
        with engine.connect() as c:
            waiting = [w["student_id"] for w in stage_state.waiting(c, 500) if w["student_id"] in mine]
        assert waiting == [str(s.id) for s in reversed(made)]

    def test_the_stage_card_carries_no_sequence_number(self, apps, world, engine):
        s = make_student(engine)
        seed_events(engine, s, ["REGISTRATION", "THOBE_ALLOCATION", "SEATING"])
        confirm(operator(apps, world, "QUEUE"), "QUEUE", token=s.token)
        with engine.connect() as c:
            card = stage_state.card_for(c, s.id)
        assert "sequence_no" not in card and "seat_no" not in card

    def test_passes_still_load_when_nobody_has_a_number(self, engine):
        mine = {insert_student(engine)[1] for _ in range(3)}
        with engine.connect() as c:
            loaded = {r["prn"] for r in passes.load_passes(c)}
        assert mine <= loaded


# ===================================================================== THE PRINTED PASS
class TestPrintedPass:
    """The pass prints a sequence line only when there is a number; never a blank one."""

    def _render(self, sequence_no):
        data = passes.PassData(name="Meera Iyer", prn="PRN-PASS-1", programme="B.Tech Computer Science",
                               token="A" * 32, sequence_no=sequence_no)
        return passes.render_single(data, "Annual Convocation 2026").pdf

    def _text(self, pdf):
        pdfium = pytest.importorskip("pypdfium2")
        return " ".join(pdfium.PdfDocument(pdf)[0].get_textpage().get_text_range().split())

    def test_a_student_with_a_number_gets_the_line(self):
        shown = self._text(self._render(1234))
        assert "1234" in shown
        assert re.search(r"SEQ", shown, re.I), shown

    def test_a_student_with_no_number_gets_no_line_and_no_blank(self):
        shown = self._text(self._render(None))
        assert not re.search(r"SEQ", shown, re.I), shown
        assert "None" not in shown
        # and the rest of the pass is untouched
        assert "Meera Iyer" in shown and "PRN-PASS-1" in shown

    def test_the_field_is_optional_so_older_callers_keep_working(self):
        data = passes.PassData(name="A", prn="B", programme="C", token="A" * 32)
        assert data.sequence_no is None

    def test_a_pass_with_no_number_still_carries_a_readable_qr(self):
        zxingcpp = pytest.importorskip("zxingcpp")
        pdfium = pytest.importorskip("pypdfium2")
        pdf = self._render(None)
        page = pdfium.PdfDocument(pdf)[0].render(scale=300 / 72).to_pil().convert("RGB")
        found = [r.text for r in zxingcpp.read_barcodes(page) if r.format == zxingcpp.BarcodeFormat.QRCode]
        assert found == ["A" * 32]

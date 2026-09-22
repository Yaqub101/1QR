"""Phase 3, the missing half: the "master patch".

THE RULE UNDER TEST (docs/TODO.md Phase 3): *"'Freeze display data' action fills `display_snapshot`;
after freeze, changes only via a logged 'master patch'."*

Once a student's display data has been frozen, that student's master fields (photo, seat, sequence
number, name, programme, school, award, master status) may be changed in exactly one way: an Admin
action that carries a mandatory reason and writes an audit row in the same transaction. There is no
second way in. The database itself refuses:

  * an UPDATE of a frozen student's master row that did not come through the patch, and
  * any UPDATE or DELETE of `display_snapshot` that did not come through freeze or a patch,

so a later phase, a stray script or a mistaken `psql` session cannot quietly rewrite what the LED
and the printed passes are built from.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend import master_patch
from backend.snapshot import freeze_display_data
from tests.admin_support import rows, scalar
from tests.test_auth import RESTRICT_VIOLATION
from tests.test_station_engine import (  # noqa: F401  (engine / world / apps are pytest fixtures)
    _CLIENTS,
    admin,
    apps,
    engine,
    make_student,
    operator,
    world,
)

REASON = "the university sent a corrected list"


@pytest.fixture(scope="module", autouse=True)
def _fresh_client_cache(world):
    _CLIENTS.clear()
    yield
    _CLIENTS.clear()


@pytest.fixture(scope="module", autouse=True)
def _admin_signed_in(apps, world, _fresh_client_cache):
    admin(apps)


# --------------------------------------------------------------------------- helpers
def frozen_student(engine, **kw):
    """A student whose display data has been frozen — the state the event runs in."""
    s = make_student(engine, **kw)
    with engine.connect() as conn:
        freeze_display_data(conn)
    return s


def snapshot_of(engine, student):
    return rows(engine, "SELECT display_name, programme, school, award, photo_path FROM display_snapshot "
                        "WHERE student_id = :s", s=student.id)[0]


def master_of(engine, student):
    return rows(engine, "SELECT name, programme, school, awards, photo_path, sequence_no, seat_no, status "
                        "FROM students WHERE id = :s", s=student.id)[0]


def patch_rows(engine, student):
    return rows(engine, "SELECT action, reason, details, operator_id, student_id FROM audit_log "
                        "WHERE student_id = :s AND action = 'MASTER_PATCH' ORDER BY id", s=student.id)


def patch(engine, student, changes, reason=REASON, operator_id=None):
    return master_patch.apply_master_patch(
        engine, student_id=str(student.id), changes=changes, reason=reason,
        operator_id=operator_id)


def form_patch(apps, student, data, client=None):
    return (client or admin(apps)).post(
        f"/admin/students/{student.id}/master-patch", data=data, follow_redirects=False)


def api_patch(apps, student, body, client=None):
    return (client or admin(apps)).post(f"/admin/api/students/{student.id}/master-patch", json=body)


# ===================================================================== THE LOCK
class TestFrozenDataIsLocked:
    def test_a_direct_update_of_a_frozen_students_master_row_is_refused_by_the_database(self, engine):
        s = frozen_student(engine)
        before = master_of(engine, s)
        with pytest.raises(DBAPIError) as caught:
            with engine.begin() as c:
                c.execute(text("UPDATE students SET seat_no = 'Z-99' WHERE id = :i"), {"i": s.id})
        assert caught.value.orig.pgcode == RESTRICT_VIOLATION
        assert master_of(engine, s) == before

    @pytest.mark.parametrize("column,value", [
        ("name", "'Someone Else'"), ("programme", "'B.A.'"), ("school", "'School of Law'"),
        ("photo_path", "'/tmp/other.jpg'"), ("awards", "'Gold Medal'"), ("sequence_no", "4242"),
        ("seat_no", "'Q-1'"), ("status", "'INACTIVE'"),
    ])
    def test_every_master_field_is_locked_once_frozen(self, engine, column, value):
        s = frozen_student(engine)
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text(f"UPDATE students SET {column} = {value} WHERE id = :i"), {"i": s.id})

    def test_a_student_who_has_not_been_frozen_can_still_be_edited_by_the_importer(self, engine):
        s = make_student(engine)  # deliberately NOT frozen
        with engine.begin() as c:
            c.execute(text("UPDATE students SET seat_no = 'A-1' WHERE id = :i"), {"i": s.id})
        assert master_of(engine, s)["seat_no"] == "A-1"

    def test_display_snapshot_cannot_be_rewritten_directly(self, engine):
        s = frozen_student(engine)
        before = snapshot_of(engine, s)
        with pytest.raises(DBAPIError) as caught:
            with engine.begin() as c:
                c.execute(text("UPDATE display_snapshot SET display_name = 'Hacked' WHERE student_id = :s"), {"s": s.id})
        assert caught.value.orig.pgcode == RESTRICT_VIOLATION
        assert snapshot_of(engine, s) == before

    def test_display_snapshot_cannot_be_deleted_directly(self, engine):
        s = frozen_student(engine)
        with pytest.raises(DBAPIError):
            with engine.begin() as c:
                c.execute(text("DELETE FROM display_snapshot WHERE student_id = :s"), {"s": s.id})
        assert snapshot_of(engine, s) is not None

    def test_running_the_freeze_again_is_still_allowed(self, engine):
        s = frozen_student(engine)
        with engine.connect() as conn:
            assert freeze_display_data(conn).frozen_count >= 1
        assert snapshot_of(engine, s)["display_name"] == s.name


# ===================================================================== THE PATCH
class TestMasterPatch:
    def test_it_changes_the_field_and_says_what_it_changed(self, engine):
        s = frozen_student(engine)
        result = patch(engine, s, {"seat_no": "B-7"})
        assert master_of(engine, s)["seat_no"] == "B-7"
        assert result.changed == {"seat_no": {"from": None, "to": "B-7"}}

    def test_several_fields_move_together_in_one_action(self, engine):
        s = frozen_student(engine)
        patch(engine, s, {"seat_no": "C-1", "sequence_no": "77", "awards": "Gold Medal"})
        after = master_of(engine, s)
        assert (after["seat_no"], after["sequence_no"], after["awards"]) == ("C-1", 77, "Gold Medal")

    def test_a_reason_is_mandatory_and_a_refusal_changes_nothing(self, engine):
        s = frozen_student(engine)
        before = master_of(engine, s)
        for bad in ("", "   ", None):
            with pytest.raises(master_patch.MasterPatchError) as caught:
                patch(engine, s, {"seat_no": "D-4"}, reason=bad)
            assert caught.value.status_code == 400
        assert master_of(engine, s) == before and patch_rows(engine, s) == []

    def test_an_absurdly_long_reason_is_refused(self, engine):
        s = frozen_student(engine)
        with pytest.raises(master_patch.MasterPatchError):
            patch(engine, s, {"seat_no": "D-4"}, reason="x" * 501)
        assert master_of(engine, s)["seat_no"] is None

    def test_the_change_and_its_audit_row_are_written_together(self, engine, world):
        s = frozen_student(engine)
        patch(engine, s, {"seat_no": "E-9"}, operator_id=world.admin_id)
        logged = patch_rows(engine, s)
        assert len(logged) == 1
        entry = logged[0]
        assert entry["reason"] == REASON
        assert str(entry["operator_id"]) == str(world.admin_id)
        assert str(entry["student_id"]) == str(s.id)
        assert entry["details"]["changes"] == {"seat_no": {"from": None, "to": "E-9"}}
        assert entry["details"]["prn"] == s.prn

    def test_nothing_is_written_when_nothing_would_change(self, engine):
        s = frozen_student(engine)
        patch(engine, s, {"seat_no": "F-1"})
        before_audit = len(patch_rows(engine, s))
        with pytest.raises(master_patch.MasterPatchError) as caught:
            patch(engine, s, {"seat_no": "F-1"})
        assert caught.value.code == "NOTHING_TO_CHANGE"
        assert len(patch_rows(engine, s)) == before_audit

    def test_the_prn_is_not_patchable_it_is_the_identity_the_import_matches_on(self, engine):
        s = frozen_student(engine)
        with pytest.raises(master_patch.MasterPatchError) as caught:
            patch(engine, s, {"prn": "SOMETHING-ELSE"})
        assert caught.value.code == "NOT_PATCHABLE"
        assert scalar(engine, "SELECT prn FROM students WHERE id = :i", i=s.id) == s.prn

    @pytest.mark.parametrize("field", ["id", "created_at", "updated_at", "nonsense"])
    def test_no_other_column_can_be_smuggled_in(self, engine, field):
        s = frozen_student(engine)
        with pytest.raises(master_patch.MasterPatchError) as caught:
            patch(engine, s, {field: "x"})
        assert caught.value.code == "NOT_PATCHABLE"

    def test_an_unknown_student_is_a_plain_refusal(self, engine):
        with pytest.raises(master_patch.MasterPatchError) as caught:
            master_patch.apply_master_patch(engine, student_id=str(uuid.uuid4()), changes={"seat_no": "A"},
                                            reason=REASON, operator_id=None)
        assert caught.value.status_code == 404

    def test_a_student_who_was_never_frozen_is_sent_back_to_the_import(self, engine):
        s = make_student(engine)
        with pytest.raises(master_patch.MasterPatchError) as caught:
            patch(engine, s, {"seat_no": "A-1"})
        assert caught.value.code == "NOT_FROZEN" and caught.value.status_code == 409

    @pytest.mark.parametrize("changes", [
        {"sequence_no": "not a number"}, {"sequence_no": "-3"}, {"name": "   "},
        {"name": "x" * 201}, {"status": "MAYBE"},
    ])
    def test_a_bad_value_is_refused_and_nothing_is_written(self, engine, changes):
        s = frozen_student(engine)
        before = master_of(engine, s)
        with pytest.raises(master_patch.MasterPatchError):
            patch(engine, s, changes)
        assert master_of(engine, s) == before and patch_rows(engine, s) == []

    def test_a_sequence_number_can_be_cleared_again(self, engine):
        s = frozen_student(engine)
        patch(engine, s, {"sequence_no": "55"})
        patch(engine, s, {"sequence_no": ""})
        assert master_of(engine, s)["sequence_no"] is None

    def test_the_students_own_journey_is_never_touched(self, engine):
        s = frozen_student(engine)
        before = scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s", s=s.id)
        patch(engine, s, {"seat_no": "G-2"})
        assert scalar(engine, "SELECT count(*) FROM activity_events WHERE student_id = :s", s=s.id) == before


# ===================================================================== THE SNAPSHOT
class TestPatchAndTheDisplaySnapshot:
    def test_a_patch_to_a_display_field_reaches_the_snapshot_the_led_reads(self, engine):
        s = frozen_student(engine)
        patch(engine, s, {"name": "Corrected Name", "awards": "Gold Medal"})
        snap = snapshot_of(engine, s)
        assert snap["display_name"] == "Corrected Name" and snap["award"] == "Gold Medal"

    def test_a_patch_to_a_field_the_led_never_shows_leaves_the_snapshot_alone(self, engine):
        s = frozen_student(engine)
        before = snapshot_of(engine, s)
        patch(engine, s, {"seat_no": "H-3", "sequence_no": "31"})
        assert snapshot_of(engine, s) == before

    def test_only_that_one_student_is_refreshed(self, engine):
        first, second = make_student(engine), make_student(engine)
        with engine.connect() as conn:
            freeze_display_data(conn)
        untouched = snapshot_of(engine, second)
        patch(engine, first, {"name": "Only Me"})
        assert snapshot_of(engine, second) == untouched
        assert snapshot_of(engine, first)["display_name"] == "Only Me"

    def test_the_audit_row_says_whether_the_led_data_moved(self, engine):
        s = frozen_student(engine)
        patch(engine, s, {"seat_no": "J-4"})
        assert patch_rows(engine, s)[-1]["details"]["snapshot_refreshed"] is False
        patch(engine, s, {"name": "New Name"})
        assert patch_rows(engine, s)[-1]["details"]["snapshot_refreshed"] is True


# ===================================================================== THE SCREEN
class TestTheScreen:
    def test_the_student_page_offers_the_patch_once_the_data_is_frozen(self, apps, engine):
        s = frozen_student(engine)
        page = admin(apps, "stadium").get(f"/admin/students/{s.id}").text
        assert "master-patch" in page
        assert "Reason" in page

    def test_the_student_page_does_not_offer_it_before_the_freeze(self, apps, engine):
        s = make_student(engine)
        assert "master-patch" not in admin(apps, "stadium").get(f"/admin/students/{s.id}").text

    def test_the_form_applies_the_change_and_says_so(self, apps, engine):
        s = frozen_student(engine)
        response = form_patch(apps, s, {"seat_no": "K-5", "reason": REASON})
        assert response.status_code == 303
        assert master_of(engine, s)["seat_no"] == "K-5"
        assert "msg=" in response.headers["location"]

    def test_the_form_refuses_without_a_reason_and_says_why(self, apps, engine):
        s = frozen_student(engine)
        response = form_patch(apps, s, {"seat_no": "L-6", "reason": "  "})
        assert response.status_code == 303 and "error=" in response.headers["location"]
        assert master_of(engine, s)["seat_no"] is None

    def test_a_blank_box_on_the_form_means_leave_it_alone_not_erase_it(self, apps, engine):
        s = frozen_student(engine)
        form_patch(apps, s, {"seat_no": "M-7", "sequence_no": "", "reason": REASON})
        form_patch(apps, s, {"name": "Renamed Student", "seat_no": "", "reason": REASON})
        after = master_of(engine, s)
        assert after["name"] == "Renamed Student" and after["seat_no"] == "M-7"

    def test_the_json_api_does_the_same_thing(self, apps, engine, world):
        s = frozen_student(engine)
        response = api_patch(apps, s, {"changes": {"seat_no": "N-8"}, "reason": REASON})
        assert response.status_code == 200, response.text
        assert response.json()["changed"] == {"seat_no": {"from": None, "to": "N-8"}}
        assert master_of(engine, s)["seat_no"] == "N-8"

    def test_the_json_api_refuses_without_a_reason(self, apps, engine):
        s = frozen_student(engine)
        response = api_patch(apps, s, {"changes": {"seat_no": "O-9"}, "reason": ""})
        assert response.status_code == 400
        assert master_of(engine, s)["seat_no"] is None

    def test_an_operator_cannot_patch_anything(self, apps, world, engine):
        s = frozen_student(engine)
        client = operator(apps, world, "SEATING")
        assert client.post(f"/admin/api/students/{s.id}/master-patch",
                           json={"changes": {"seat_no": "P-1"}, "reason": REASON}).status_code == 403
        assert master_of(engine, s)["seat_no"] is None

    def test_a_signed_out_visitor_cannot_patch_anything(self, apps, engine):
        from tests.test_auth import new_client
        s = frozen_student(engine)
        anonymous = new_client(apps)
        assert anonymous.post(f"/admin/api/students/{s.id}/master-patch",
                              json={"changes": {"seat_no": "P-2"}, "reason": REASON}).status_code == 401

    def test_the_patch_shows_up_on_the_audit_trail_screen(self, apps, engine, world):
        s = frozen_student(engine)
        patch(engine, s, {"seat_no": "R-3"}, operator_id=world.admin_id)
        page = admin(apps, "stadium").get(f"/admin/audit?student={s.prn}").text
        assert "MASTER_PATCH" in page and REASON in page

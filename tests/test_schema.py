"""Phase 2 - database schema tests.

Every rule below must hold at the DATABASE level: the tests talk to PostgreSQL
with raw SQL and never go through the API layer, so a bug or a hostile request
that bypasses the application still cannot break the rules (SYSTEM_SPEC
sections 5, 15, 17; AGENTS.md golden rules 3, 5, as amended by
docs/ARCHITECTURE_PIVOT.md).

The schema under test is built by the single documented command,
``alembic upgrade head``, run as a subprocess against the TEST database only.
"""
import itertools
import json
import random
import threading
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import (  # the one place a schema is built: the documented `alembic upgrade head`
    REPO_ROOT,
    TEST_DB_URL,
    _assert_is_test_database,
    drop_everything,
    rebuild_schema,
    run_alembic,
)

# PostgreSQL SQLSTATE codes
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"
FK_VIOLATION = "23503"
NOT_NULL_VIOLATION = "23502"
RESTRICT_VIOLATION = "23001"  # used by our append-only / immutability triggers

ACTIVITIES = [
    "REGISTRATION",
    "THOBE_ALLOCATION",
    "SEATING",
    "QUEUE",
    "STAGE",
    "THOBE_RETURN",
    "LUNCH",
]

# SYSTEM_SPEC section 5: label after completing each step (index 0 = nothing done)
STATUS_AFTER_STEP = [
    "REGISTERED / NOT REPORTED",
    "REPORTED / ROBE NOT RECEIVED",
    "NOT SEATED",
    "NOT QUEUED",
    "DEGREE NOT RECEIVED",
    "ROBE NOT RETURNED",
    "LUNCH ELIGIBLE",
    "EXITED",
]

EXPECTED_TABLES = {
    "students",
    "display_snapshot",
    "qr_tokens",
    "activity_events",
    "scan_log",
    "users",
    "sessions",
    "queue",
    "exceptions",
    "audit_log",
    "settings",
    "counters",  # the gap-free counter backing queue_position
}


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_size=40, max_overflow=0, pool_pre_ping=True)
    rebuild_schema(eng)
    yield eng
    drop_everything(eng)
    eng.dispose()


@pytest.fixture
def conn(engine):
    """A connection inside one outer transaction that is always rolled back."""
    connection = engine.connect()
    outer = connection.begin()
    try:
        yield connection
    finally:
        outer.rollback()
        connection.close()


@pytest.fixture
def committed_db(engine):
    """Pristine schema for tests that need real commits (concurrency)."""
    rebuild_schema(engine)
    yield engine
    rebuild_schema(engine)


@contextmanager
def db_error(conn, code, match=None):
    """Assert the body raises a database error with the given SQLSTATE."""
    with pytest.raises(DBAPIError) as exc_info:
        with conn.begin_nested():  # savepoint keeps the outer transaction usable
            yield
    orig = exc_info.value.orig
    assert orig.pgcode == code, f"expected SQLSTATE {code}, got {orig.pgcode}: {orig}"
    if match:
        assert match.lower() in str(orig).lower(), f"{match!r} not in {orig}"


_seq = itertools.count(1)


def new_student(conn, **overrides):
    n = next(_seq)
    row = {
        "prn": f"PRN{n:06d}",
        "name": f"Student {n}",
        "programme": "B.Tech",
        "school": "School of Engineering",
        "sequence_no": n,
    }
    row.update(overrides)
    return conn.execute(
        text(
            "INSERT INTO students (prn, name, programme, school, sequence_no) "
            "VALUES (:prn, :name, :programme, :school, :sequence_no) RETURNING id"
        ),
        row,
    ).scalar_one()


def new_user(conn, role="ADMIN", username=None):
    return conn.execute(
        text("INSERT INTO users (username, password_hash, role) VALUES (:u, 'x', :r) RETURNING id"),
        {"u": username or f"user-{uuid.uuid4().hex[:8]}", "r": role},
    ).scalar_one()


def add_event(
    conn,
    student_id,
    activity,
    *,
    kind="COMPLETE",
    flags=None,
    details=None,
    cycle=1,
    corrects=None,
    operator=None,
    event_id=None,
):
    """Insert one activity event; returns event_id."""
    if details is None:
        details = {} if kind == "COMPLETE" else {"reason": "test reason"}
    if flags is None:
        flags = ["CORRECTED"] if kind == "WAIVER" else []
    row = conn.execute(
        text(
            "INSERT INTO activity_events (event_id, student_id, activity, kind, "
            "operator_id, flags, details, completion_cycle, corrects_event_id) "
            "VALUES (COALESCE(:event_id, gen_random_uuid()), :student_id, :activity, :kind, "
            ":operator, CAST(:flags AS text[]), CAST(:details AS jsonb), "
            ":cycle, :corrects) RETURNING event_id"
        ),
        {
            "event_id": event_id,
            "student_id": student_id,
            "activity": activity,
            "kind": kind,
            "operator": operator or uuid.uuid4(),
            "flags": list(flags),
            "details": json.dumps(details),
            "cycle": cycle,
            "corrects": corrects,
        },
    ).one()
    return row.event_id


def status_of(conn, student_id):
    return conn.execute(
        text("SELECT status FROM student_status WHERE student_id = :s"), {"s": student_id}
    ).scalar_one()


def complete_steps(conn, student_id, n):
    """Complete the first n activities of the journey, in order."""
    for activity in ACTIVITIES[:n]:
        add_event(conn, student_id, activity)


# --------------------------------------------------------------------------- #
# Migrations: rebuild from empty with the single documented command
# --------------------------------------------------------------------------- #
class TestMigrations:
    def test_schema_rebuilds_from_empty_with_single_documented_command(self, engine):
        drop_everything(engine)
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname='public'")).scalar_one() == 0

        result = run_alembic("upgrade", "head")  # README: `alembic upgrade head`
        assert result.returncode == 0, result.stdout + result.stderr

        with engine.connect() as c:
            tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
            views = {r[0] for r in c.execute(text("SELECT viewname FROM pg_views WHERE schemaname='public'"))}
        assert EXPECTED_TABLES <= tables, f"missing tables: {EXPECTED_TABLES - tables}"
        assert "student_status" in views

    def test_upgrade_is_repeatable(self, engine):
        assert run_alembic("upgrade", "head").returncode == 0  # already at head: no-op

    def test_the_pivot_migration_refuses_to_downgrade(self, engine):
        """0012 (docs/ARCHITECTURE_PIVOT.md) is deliberately one-way: the venue/sync/station data
        it drops cannot be reconstructed. Downgrading past it fails loudly, not silently."""
        result = run_alembic("downgrade", "0011_email_mobile")
        assert result.returncode != 0 and "NotImplementedError" in result.stderr
        assert run_alembic("upgrade", "head").returncode == 0  # still at head: the failed downgrade rolled back

    def test_single_migration_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1

    def test_required_indexes_exist(self, engine):
        def index_defs(table):
            with engine.connect() as c:
                return [r[0] for r in c.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t"), {"t": table})]

        assert any("UNIQUE" in d and "(prn)" in d for d in index_defs("students"))
        assert any("UNIQUE" in d and "(token)" in d for d in index_defs("qr_tokens"))
        assert any("UNIQUE" not in d and "(student_id, activity)" in d for d in index_defs("activity_events"))

    def test_seeded_settings_row_exists(self, conn):
        row = conn.execute(text("SELECT id FROM settings")).all()
        assert [tuple(r) for r in row] == [(1,)]


# --------------------------------------------------------------------------- #
# students / qr_tokens
# --------------------------------------------------------------------------- #
class TestStudents:
    def test_duplicate_prn_insert_fails(self, conn):
        new_student(conn, prn="DUP-PRN")
        with db_error(conn, UNIQUE_VIOLATION):
            new_student(conn, prn="DUP-PRN")

    def test_a_sequence_number_may_be_missing_or_repeated(self, conn):
        """The university's real list has no Convocation Sequence Number column. The column is kept for
        the day they supply one, but nothing requires it and nothing collides on it (migration 0010)."""
        new_student(conn, sequence_no=777_001)
        new_student(conn, sequence_no=777_001)   # the same number again: allowed
        new_student(conn, sequence_no=None)
        new_student(conn, sequence_no=None)      # and any number of students with none at all
        assert conn.execute(text("SELECT count(*) FROM students WHERE sequence_no = 777001")).scalar_one() == 2

    def test_a_sequence_number_that_is_given_must_still_be_positive(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            new_student(conn, sequence_no=0)

    def test_blank_prn_and_invalid_status_rejected(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            new_student(conn, prn="   ")
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(
                text("INSERT INTO students (prn, name, programme, school, sequence_no, status) "
                     "VALUES ('S-BAD', 'n', 'p', 's', 888001, 'GONE')"))


class TestQrTokens:
    def _token(self):
        return uuid.uuid4().hex + uuid.uuid4().hex[:4]

    def _add(self, conn, student, token=None, active=True, **cols):
        return conn.execute(
            text("INSERT INTO qr_tokens (student_id, token, active, deactivated_at, deactivated_by) "
                 "VALUES (:s, :t, :a, :da, :db) RETURNING id"),
            {"s": student, "t": token or self._token(), "a": active,
             "da": cols.get("deactivated_at"), "db": cols.get("deactivated_by")},
        ).scalar_one()

    def test_duplicate_token_insert_fails(self, conn):
        s1, s2 = new_student(conn), new_student(conn)
        self._add(conn, s1, token="SAME-TOKEN-VALUE-0000001")
        with db_error(conn, UNIQUE_VIOLATION):
            self._add(conn, s2, token="SAME-TOKEN-VALUE-0000001")

    def test_second_active_token_for_same_student_fails(self, conn):
        s = new_student(conn)
        self._add(conn, s)
        with db_error(conn, UNIQUE_VIOLATION):
            self._add(conn, s)

    def test_reissue_flow_old_deactivated_then_new_active_is_allowed(self, conn):
        s, admin = new_student(conn), new_user(conn)
        old = self._add(conn, s)
        conn.execute(
            text("UPDATE qr_tokens SET active=false, deactivated_at=now(), deactivated_by=:a WHERE id=:i"),
            {"a": admin, "i": old})
        self._add(conn, s)  # a new active token for the same student now works
        n_active = conn.execute(
            text("SELECT count(*) FROM qr_tokens WHERE student_id=:s AND active"), {"s": s}).scalar_one()
        assert n_active == 1

    def test_inactive_token_must_record_when_and_by_whom(self, conn):
        s = new_student(conn)
        with db_error(conn, CHECK_VIOLATION):
            self._add(conn, s, active=False)  # no deactivated_at / deactivated_by
        with db_error(conn, CHECK_VIOLATION):
            self._add(conn, s, active=True, deactivated_at="2026-10-01T10:00:00+00:00")

    def test_token_cannot_be_reactivated_or_rewritten(self, conn):
        s, admin = new_student(conn), new_user(conn)
        tid = self._add(conn, s)
        with db_error(conn, RESTRICT_VIOLATION):
            conn.execute(text("UPDATE qr_tokens SET token='another-token-value-00001' WHERE id=:i"), {"i": tid})
        conn.execute(
            text("UPDATE qr_tokens SET active=false, deactivated_at=now(), deactivated_by=:a WHERE id=:i"),
            {"a": admin, "i": tid})
        with db_error(conn, RESTRICT_VIOLATION):
            conn.execute(
                text("UPDATE qr_tokens SET active=true, deactivated_at=NULL, deactivated_by=NULL WHERE id=:i"),
                {"i": tid})

    def test_token_rows_cannot_be_deleted(self, conn):
        s = new_student(conn)
        tid = self._add(conn, s)
        with db_error(conn, RESTRICT_VIOLATION):
            conn.execute(text("DELETE FROM qr_tokens WHERE id=:i"), {"i": tid})


# --------------------------------------------------------------------------- #
# activity_events: duplicate prevention (SYSTEM_SPEC 15)
# --------------------------------------------------------------------------- #
class TestActivityEventDuplicatePrevention:
    @pytest.mark.parametrize("activity", ACTIVITIES)
    def test_second_complete_for_same_student_and_activity_fails_at_db_level(self, conn, activity):
        s = new_student(conn)
        add_event(conn, s, activity)
        with db_error(conn, UNIQUE_VIOLATION):
            add_event(conn, s, activity)  # different event_id, same (student, activity)
        n = conn.execute(
            text("SELECT count(*) FROM activity_events WHERE student_id=:s AND activity=:a"),
            {"s": s, "a": activity}).scalar_one()
        assert n == 1

    def test_one_student_can_complete_all_seven_activities(self, conn):
        s = new_student(conn)
        for activity in ACTIVITIES:
            add_event(conn, s, activity)
        got = conn.execute(
            text("SELECT activity FROM activity_events WHERE student_id=:s AND kind='COMPLETE'"), {"s": s}).scalars().all()
        assert sorted(got) == sorted(ACTIVITIES)

    def test_duplicates_are_per_activity_and_per_student(self, conn):
        a, b = new_student(conn), new_student(conn)
        add_event(conn, a, "REGISTRATION")
        add_event(conn, b, "REGISTRATION")       # same activity, other student: fine
        add_event(conn, a, "THOBE_ALLOCATION")   # same student, other activity: fine

    def test_replaying_the_same_event_id_is_rejected(self, conn):
        s = new_student(conn)
        eid = add_event(conn, s, "REGISTRATION")
        with db_error(conn, UNIQUE_VIOLATION):
            add_event(conn, s, "REGISTRATION", event_id=eid)

    def test_return_waiver_and_normal_return_are_mutually_exclusive(self, conn):
        s = new_student(conn)
        add_event(conn, s, "THOBE_RETURN", kind="WAIVER")
        with db_error(conn, UNIQUE_VIOLATION):
            add_event(conn, s, "THOBE_RETURN", kind="COMPLETE")

    def test_stage_skips_do_not_use_up_the_completion_slot(self, conn):
        s = new_student(conn)
        add_event(conn, s, "STAGE", kind="SKIP")
        add_event(conn, s, "STAGE", kind="SKIP", details={"reason": "not ready"})
        add_event(conn, s, "STAGE", kind="COMPLETE")


class TestActivityEventShape:
    def test_flags_are_a_set_that_can_carry_several_values(self, conn):
        s = new_student(conn)
        add_event(conn, s, "REGISTRATION", flags=["PROVISIONAL", "MANUAL", "LATE", "CORRECTED"])
        flags = conn.execute(
            text("SELECT flags FROM activity_events WHERE student_id=:s"), {"s": s}).scalar_one()
        assert set(flags) == {"PROVISIONAL", "MANUAL", "LATE", "CORRECTED"}

    def test_flags_default_to_empty_and_reject_unknown_values(self, conn):
        s1, s2, s3 = new_student(conn), new_student(conn), new_student(conn)
        add_event(conn, s1, "REGISTRATION")
        assert conn.execute(
            text("SELECT flags FROM activity_events WHERE student_id=:s"), {"s": s1}).scalar_one() == []
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s2, "REGISTRATION", flags=["MANUAL", "BOGUS"])
        with db_error(conn, NOT_NULL_VIOLATION):
            conn.execute(text(
                "INSERT INTO activity_events (student_id, activity, operator_id, flags) "
                "VALUES (:s, 'REGISTRATION', gen_random_uuid(), NULL)"), {"s": s3})

    def test_unknown_activity_kind_and_student_are_rejected(self, conn):
        s = new_student(conn)
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "EXIT")
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "LUNCH", kind="ERASE")
        with db_error(conn, FK_VIOLATION):
            add_event(conn, uuid.uuid4(), "REGISTRATION")

    def test_skip_is_stage_only_and_waiver_is_thobe_return_only(self, conn):
        s = new_student(conn)
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="SKIP")
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "LUNCH", kind="WAIVER")

    def test_skip_waiver_and_reversal_require_a_reason(self, conn):
        s = new_student(conn)
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "STAGE", kind="SKIP", details={})
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "THOBE_RETURN", kind="WAIVER", details={"reason": "   "})
        eid = add_event(conn, s, "SEATING")
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="REVERSAL", corrects=eid, details={})

    def test_waiver_must_carry_the_corrected_flag(self, conn):
        s = new_student(conn)
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "THOBE_RETURN", kind="WAIVER", flags=[])

    def test_operator_is_mandatory(self, conn):
        s = new_student(conn)
        with db_error(conn, NOT_NULL_VIOLATION):
            conn.execute(text(
                "INSERT INTO activity_events (student_id, activity, operator_id) "
                "VALUES (:s, 'REGISTRATION', NULL)"), {"s": s})


class TestReversals:
    """Corrections are new events; a reversal re-opens the slot for exactly one new completion."""

    def test_reversal_then_new_completion_is_allowed(self, conn):
        s = new_student(conn)
        first = add_event(conn, s, "STAGE")
        add_event(conn, s, "STAGE", kind="REVERSAL", corrects=first, cycle=1)
        add_event(conn, s, "STAGE", cycle=2)  # accidental COMPLETE undone, then done properly
        rows = conn.execute(
            text("SELECT kind, completion_cycle FROM activity_events WHERE student_id=:s ORDER BY server_time"),
            {"s": s}).all()
        assert [tuple(r) for r in rows] == [("COMPLETE", 1), ("REVERSAL", 1), ("COMPLETE", 2)]  # original kept

    def test_second_completion_without_a_reversal_is_rejected(self, conn):
        s = new_student(conn)
        add_event(conn, s, "STAGE")
        with db_error(conn, CHECK_VIOLATION, match="reversed"):
            add_event(conn, s, "STAGE", cycle=2)  # cannot skip the duplicate guard by bumping the cycle

    def test_reversal_must_point_at_a_real_completion_of_the_same_student_and_activity(self, conn):
        s, other = new_student(conn), new_student(conn)
        seating = add_event(conn, s, "SEATING")
        other_reg = add_event(conn, other, "REGISTRATION")
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="REVERSAL", corrects=uuid.uuid4(), cycle=1)   # no such event
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="REVERSAL", corrects=other_reg, cycle=1)      # someone else's
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="REVERSAL", corrects=seating, cycle=2)        # wrong cycle
        with db_error(conn, CHECK_VIOLATION):
            add_event(conn, s, "SEATING", kind="REVERSAL", corrects=None, cycle=1)           # no reference

    def test_a_completion_can_only_be_reversed_once(self, conn):
        s = new_student(conn)
        first = add_event(conn, s, "QUEUE")
        add_event(conn, s, "QUEUE", kind="REVERSAL", corrects=first, cycle=1)
        with db_error(conn, UNIQUE_VIOLATION):
            add_event(conn, s, "QUEUE", kind="REVERSAL", corrects=first, cycle=1)

    def test_new_completion_after_reversal_is_still_unique(self, conn):
        s = new_student(conn)
        first = add_event(conn, s, "QUEUE")
        add_event(conn, s, "QUEUE", kind="REVERSAL", corrects=first, cycle=1)
        add_event(conn, s, "QUEUE", cycle=2)
        with db_error(conn, UNIQUE_VIOLATION):
            add_event(conn, s, "QUEUE", cycle=2)


# --------------------------------------------------------------------------- #
# Append-only guarantees (golden rule 5, SYSTEM_SPEC 17)
# --------------------------------------------------------------------------- #
def add_audit(conn, action="EVENT_CONFIRMED", **cols):
    return conn.execute(
        text("INSERT INTO audit_log (action, student_id, activity, operator_id, reason) "
             "VALUES (:action, :student_id, :activity, :operator_id, :reason) RETURNING id"),
        {"action": action, "student_id": cols.get("student_id"), "activity": cols.get("activity"),
         "operator_id": cols.get("operator_id"), "reason": cols.get("reason")},
    ).scalar_one()


class TestAuditLogAppendOnly:
    def test_insert_is_allowed(self, conn):
        s, u = new_student(conn), new_user(conn)
        add_audit(conn, student_id=s, activity="REGISTRATION", operator_id=u)

    def test_update_raises(self, conn):
        aid = add_audit(conn)
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("UPDATE audit_log SET action='TAMPERED' WHERE id=:i"), {"i": aid})

    def test_delete_raises(self, conn):
        aid = add_audit(conn)
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("DELETE FROM audit_log WHERE id=:i"), {"i": aid})

    def test_truncate_raises(self, conn):
        add_audit(conn)
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("TRUNCATE audit_log"))

    def test_row_is_unchanged_after_blocked_tampering(self, conn):
        aid = add_audit(conn, action="ORIGINAL")
        with db_error(conn, RESTRICT_VIOLATION):
            conn.execute(text("UPDATE audit_log SET action='TAMPERED'"))
        assert conn.execute(text("SELECT action FROM audit_log WHERE id=:i"), {"i": aid}).scalar_one() == "ORIGINAL"

    def test_a_correction_needs_a_correcting_admin_and_a_reason(self, conn):
        admin = new_user(conn)
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO audit_log (action, corrects_event_id) VALUES ('CORRECTION', gen_random_uuid())"))
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text(
                "INSERT INTO audit_log (action, corrects_event_id, corrected_by, reason) "
                "VALUES ('CORRECTION', gen_random_uuid(), :a, '  ')"), {"a": admin})
        conn.execute(text(
            "INSERT INTO audit_log (action, corrects_event_id, corrected_by, reason) "
            "VALUES ('CORRECTION', gen_random_uuid(), :a, 'wrong student confirmed')"), {"a": admin})


class TestActivityEventsAppendOnly:
    def test_update_raises(self, conn):
        s = new_student(conn)
        eid = add_event(conn, s, "REGISTRATION")
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("UPDATE activity_events SET flags='{}' WHERE event_id=:e"), {"e": eid})

    def test_delete_raises(self, conn):
        s = new_student(conn)
        eid = add_event(conn, s, "REGISTRATION")
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("DELETE FROM activity_events WHERE event_id=:e"), {"e": eid})

    def test_truncate_raises(self, conn):
        add_event(conn, new_student(conn), "REGISTRATION")
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("TRUNCATE activity_events"))

    def test_deleting_a_student_who_has_events_is_blocked(self, conn):
        s = new_student(conn)
        add_event(conn, s, "REGISTRATION")
        with db_error(conn, FK_VIOLATION):
            conn.execute(text("DELETE FROM students WHERE id=:s"), {"s": s})


class TestScanLogAppendOnly:
    def _scan(self, conn, result="SUCCESS"):
        return conn.execute(
            text("INSERT INTO scan_log (activity, result) VALUES ('REGISTRATION', :r) RETURNING id"), {"r": result}).scalar_one()

    @pytest.mark.parametrize("result", ["READY", "SUCCESS", "DUPLICATE", "INVALID", "REJECTED", "PROVISIONAL", "MANUAL"])
    def test_every_result_kind_can_be_logged(self, conn, result):
        # READY is the successful SCAN: the student was identified and shown to the operator, who has not
        # confirmed yet. Phase 6 requires every attempt in the log, and that is the commonest attempt of all.
        self._scan(conn, result)

    def test_unknown_result_rejected(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            self._scan(conn, "MAYBE")

    def test_update_and_delete_raise(self, conn):
        sid = self._scan(conn)
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("UPDATE scan_log SET result='SUCCESS' WHERE id=:i"), {"i": sid})
        with db_error(conn, RESTRICT_VIOLATION, match="append-only"):
            conn.execute(text("DELETE FROM scan_log WHERE id=:i"), {"i": sid})


def _run_threads(fn, n):
    """Run fn(i) in n threads released together; return list of (result | exception)."""
    barrier = threading.Barrier(n)
    out = [None] * n

    def worker(i):
        try:
            barrier.wait(timeout=30)
            out[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - the exception IS the result we assert on
            out[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return out


class TestConcurrency:
    """Real commits from many connections at once (uses a freshly rebuilt schema)."""

    def _students(self, engine, n):
        with engine.begin() as c:
            return [new_student(c) for _ in range(n)]

    def test_racing_duplicate_completions_leave_exactly_one_row(self, committed_db):
        engine, n = committed_db, 10
        (student,) = self._students(engine, 1)

        def work(_):
            with engine.begin() as c:
                return add_event(c, student, "REGISTRATION")

        results = _run_threads(work, n)
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, DBAPIError)]
        assert len(winners) == 1 and len(losers) == n - 1, results
        assert all(r.orig.pgcode == UNIQUE_VIOLATION for r in losers)
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM activity_events")).scalar_one() == 1

    def test_concurrent_queue_confirmations_get_positions_one_to_n(self, committed_db):
        engine, n = committed_db, 15
        students = self._students(engine, n)

        def work(i):
            with engine.begin() as c:
                return c.execute(
                    text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                    {"s": students[i]}).scalar_one()

        results = _run_threads(work, n)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert sorted(results) == list(range(1, n + 1))


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #
class TestQueue:
    def test_positions_are_assigned_by_the_counter_in_confirmation_order(self, conn):
        students = [new_student(conn) for _ in range(3)]
        got = [conn.execute(text("INSERT INTO queue (student_id) VALUES (:s) RETURNING queue_position"),
                            {"s": s}).scalar_one() for s in students]
        assert got == [1, 2, 3]

    def test_a_student_can_be_queued_only_once(self, conn):
        s = new_student(conn)
        conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s})
        with db_error(conn, UNIQUE_VIOLATION):
            conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s})

    def test_position_never_changes_after_insert(self, conn):
        s = new_student(conn)
        conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s})
        with db_error(conn, RESTRICT_VIOLATION):
            conn.execute(text("UPDATE queue SET queue_position = 99 WHERE student_id=:s"), {"s": s})

    @pytest.mark.parametrize("status", ["QUEUED", "DISPLAYED", "DONE", "SKIPPED", "HELD"])
    def test_status_can_move_through_every_state(self, conn, status):
        s = new_student(conn)
        conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s})
        conn.execute(text("UPDATE queue SET status=:st WHERE student_id=:s"), {"st": status, "s": s})

    def test_unknown_status_rejected(self, conn):
        s = new_student(conn)
        conn.execute(text("INSERT INTO queue (student_id) VALUES (:s)"), {"s": s})
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("UPDATE queue SET status='LOST' WHERE student_id=:s"), {"s": s})


# --------------------------------------------------------------------------- #
# exceptions / settings / users / display_snapshot
# --------------------------------------------------------------------------- #
class TestSmallTables:
    def test_exceptions_start_open_and_resolution_is_consistent(self, conn):
        eid = conn.execute(text("INSERT INTO exceptions (type) VALUES ('RETURN_WAIVED') RETURNING id")).scalar_one()
        assert conn.execute(text("SELECT status FROM exceptions WHERE id=:i"), {"i": eid}).scalar_one() == "OPEN"
        with db_error(conn, CHECK_VIOLATION):  # RESOLVED must say when
            conn.execute(text("UPDATE exceptions SET status='RESOLVED' WHERE id=:i"), {"i": eid})
        with db_error(conn, CHECK_VIOLATION):  # OPEN must not carry a resolution
            conn.execute(text("UPDATE exceptions SET resolved_at=now() WHERE id=:i"), {"i": eid})
        conn.execute(text("UPDATE exceptions SET status='RESOLVED', resolved_at=now() WHERE id=:i"), {"i": eid})

    def test_exceptions_reject_blank_type_and_unknown_status(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO exceptions (type) VALUES ('  ')"))
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO exceptions (type, status) VALUES ('RETURN_WAIVED', 'IGNORED')"))

    def test_settings_is_a_single_row(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            conn.execute(text("INSERT INTO settings (id) VALUES (2)"))
        with db_error(conn, UNIQUE_VIOLATION):
            conn.execute(text("INSERT INTO settings (id) VALUES (1)"))

    @pytest.mark.parametrize("role", ["ADMIN", "REGISTRATION", "THOBE_ALLOCATION", "SEATING",
                                      "QUEUE", "STAGE", "THOBE_RETURN", "LUNCH"])
    def test_every_spec_role_is_accepted(self, conn, role):
        new_user(conn, role=role)

    def test_users_reject_unknown_role_and_case_insensitive_duplicate_usernames(self, conn):
        with db_error(conn, CHECK_VIOLATION):
            new_user(conn, role="SUPERUSER")
        new_user(conn, username="Deputy.Admin")
        with db_error(conn, UNIQUE_VIOLATION):
            new_user(conn, username="deputy.admin")

    def test_display_snapshot_holds_only_led_fields_and_one_row_per_student(self, conn):
        s = new_student(conn)
        conn.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school) "
                          "VALUES (:s, 'A. Student', 'B.Tech', 'Engineering')"), {"s": s})
        with db_error(conn, UNIQUE_VIOLATION):
            conn.execute(text("INSERT INTO display_snapshot (student_id, display_name, programme, school) "
                              "VALUES (:s, 'A. Student', 'B.Tech', 'Engineering')"), {"s": s})
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='display_snapshot'"))}
        assert not ({"prn", "phone", "email", "sequence_no", "seat_no"} & cols)  # golden rule 10


# --------------------------------------------------------------------------- #
# student_status: derived from events, never stored (SYSTEM_SPEC 5)
# --------------------------------------------------------------------------- #
class TestStudentStatus:
    def test_view_exposes_step_and_label(self, conn):
        s = new_student(conn)
        row = conn.execute(text("SELECT step, status FROM student_status WHERE student_id=:s"), {"s": s}).one()
        assert (row.step, row.status) == (0, STATUS_AFTER_STEP[0])

    @pytest.mark.parametrize("n_done", range(8), ids=[f"after_{n}_steps" for n in range(8)])
    def test_label_at_every_stage_of_the_journey(self, conn, n_done):
        s = new_student(conn)
        complete_steps(conn, s, n_done)
        assert status_of(conn, s) == STATUS_AFTER_STEP[n_done]

    def test_each_transition_advances_the_label_exactly_one_state(self, conn):
        s = new_student(conn)
        seen = [status_of(conn, s)]
        for activity in ACTIVITIES:
            add_event(conn, s, activity)
            seen.append(status_of(conn, s))
        assert seen == STATUS_AFTER_STEP

    def test_registration_moves_not_reported_to_reported(self, conn):
        s = new_student(conn)
        assert status_of(conn, s) == "REGISTERED / NOT REPORTED"
        add_event(conn, s, "REGISTRATION")
        assert status_of(conn, s) == "REPORTED / ROBE NOT RECEIVED"

    def test_thobe_allocation_moves_to_not_seated(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 1)
        add_event(conn, s, "THOBE_ALLOCATION")
        assert status_of(conn, s) == "NOT SEATED"

    def test_seating_moves_to_not_queued(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 2)
        add_event(conn, s, "SEATING")
        assert status_of(conn, s) == "NOT QUEUED"

    def test_queue_moves_to_degree_not_received(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 3)
        add_event(conn, s, "QUEUE")
        assert status_of(conn, s) == "DEGREE NOT RECEIVED"

    def test_stage_complete_moves_to_thobe_not_returned(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 4)
        add_event(conn, s, "STAGE")
        assert status_of(conn, s) == "ROBE NOT RETURNED"

    def test_thobe_return_moves_to_lunch_eligible(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 5)
        add_event(conn, s, "THOBE_RETURN")
        assert status_of(conn, s) == "LUNCH ELIGIBLE"

    def test_lunch_moves_to_exited(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 6)
        add_event(conn, s, "LUNCH")
        assert status_of(conn, s) == "EXITED"

    def test_stage_skip_does_not_count_as_degree_received(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 4)
        add_event(conn, s, "STAGE", kind="SKIP")
        assert status_of(conn, s) == "DEGREE NOT RECEIVED"

    def test_admin_waiver_counts_as_thobe_return(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 5)
        add_event(conn, s, "THOBE_RETURN", kind="WAIVER")
        assert status_of(conn, s) == "LUNCH ELIGIBLE"

    def test_reversal_steps_the_status_back_and_recompletion_restores_it(self, conn):
        s = new_student(conn)
        complete_steps(conn, s, 4)
        stage = add_event(conn, s, "STAGE")
        assert status_of(conn, s) == "ROBE NOT RETURNED"
        add_event(conn, s, "STAGE", kind="REVERSAL", corrects=stage, cycle=1)
        assert status_of(conn, s) == "DEGREE NOT RECEIVED"
        add_event(conn, s, "STAGE", cycle=2)
        assert status_of(conn, s) == "ROBE NOT RETURNED"

    def test_status_is_identical_whatever_order_events_arrive_in(self, conn):
        rng = random.Random(20261015)
        for n_done in (4, 7):
            for _ in range(6):
                s = new_student(conn)
                order = ACTIVITIES[:n_done][:]
                rng.shuffle(order)
                for activity in order:
                    add_event(conn, s, activity)
                assert status_of(conn, s) == STATUS_AFTER_STEP[n_done]

    def test_a_flagged_event_shows_the_furthest_step_reached(self, conn):
        # A flag on an event (e.g. MANUAL, LATE) never changes what the status view derives from it.
        s = new_student(conn)
        add_event(conn, s, "LUNCH", flags=["MANUAL"])
        assert status_of(conn, s) == "EXITED"

    def test_students_are_independent(self, conn):
        a, b = new_student(conn), new_student(conn)
        complete_steps(conn, a, 3)
        assert (status_of(conn, a), status_of(conn, b)) == (STATUS_AFTER_STEP[3], STATUS_AFTER_STEP[0])

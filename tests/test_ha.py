"""Phase 17: high availability. Backups, restore, rebuilding central, and the automated schedule.

What is proven here, on the real PostgreSQL tools (`pg_dump` / `pg_restore`) and real separate databases:

  * RESTORE DRILL       A populated database is dumped, WIPED (dropped), restored from the dump alone, and every report
                        from the previous bundle regenerates IDENTICALLY; the app then runs on the restored data, the
                        append-only triggers are still in force, and the gap-free counters carry on.
  * REBUILD CENTRAL     A real sync topology fills central; central is wiped; `rebuild_central` reconstructs it from the
                        venues' own databases; counts and derived status match; venues re-pull without duplicating.
  * BACKUP SCHEDULE     Every 5 minutes (documented default), fast-forwarded with a fake clock rather than waited for.
                        Retention never removes a milestone; a failed run is retried; missed intervals are not replayed.

What CANNOT be proven here (no second machine, no real network): see docs/HA.md, "What still needs real hardware".
"""
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from backend import database
from backend import users as users_svc
from backend.admin import dashboard, reports
from backend.config import Settings
from backend.ha import backup, pgtools, restore
from backend.main import create_app
from backend.sync.keys import issue_key
from backend.sync.rebuild import rebuild_central
from tests.admin_support import build_dataset
from tests.sync_support import Stack, forget_engine, status_rows, url_for
from tests.test_auth import PASSWORD, RESTRICT_VIOLATION, api_login, new_client
from tests.conftest import run_alembic
from tests.test_schema import REPO_ROOT
from tests.test_sync import register

FAILOVER_DIR = pathlib.Path(REPO_ROOT) / "docs" / "failover"


@pytest.fixture(scope="module")
def stack():
    s = Stack("ha", students=40)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_slate(stack):
    stack.reset()
    yield
    stack.stop_central()


def everything(engine, settings, sample_student):
    """Every report from the previous bundle, the dashboard and the derived status, as plain comparable data."""
    out = {}
    with engine.connect() as conn:
        for key in reports.REPORTS:
            params = {"student_id": str(sample_student)} if key == "student-history" else {}
            r = reports.run(conn, settings, key, params)
            out[key] = {"columns": r.columns, "rows": r.rows, "totals": r.totals, "note": r.note}
    with engine.connect() as conn:
        snap = dashboard.snapshot(conn, settings)
    snap.pop("as_of")                                   # the only field that is "now" by design
    out["dashboard"] = json.loads(json.dumps(snap, default=str))
    out["status"] = status_rows(engine)
    with engine.connect() as conn:
        out["table_counts"] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in
                               ("students", "activity_events", "audit_log", "scan_log", "exceptions", "outbox", "queue", "qr_tokens")}
    return out


def drop_db(name):
    from tests.sync_support import drop_database
    drop_database(name)


def database_exists(name) -> bool:
    admin = create_engine(pgtools.maintenance_url(url_for(name)), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            return bool(c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar())
    finally:
        admin.dispose()


# =========================================================================== THE RESTORE DRILL
class TestRestoreDrill:
    def test_dump_wipe_restore_and_every_report_regenerates_identically(self, stack, tmp_path):
        engine = stack.scratch("drill")
        url = stack.urls["scratch-drill"]
        name = stack.names["scratch-drill"]
        data = build_dataset(engine)                                   # 22 students with a known answer, on top of the 40 seeded
        settings = Settings(mode="venue", venue_id="hall", database_url=url)
        sample = data.people["L"].s.id
        before = everything(engine, settings, sample)
        assert before["table_counts"]["activity_events"] > 60 and len(before["status"]) == 62
        assert set(reports.REPORTS) <= set(before)                     # every report from the previous bundle is in the comparison

        info = backup.create_backup(url, str(tmp_path), kind="milestone", label="before-event")
        assert pathlib.Path(info.path).stat().st_size > 10_000 and info.counts["activity_events"] == before["table_counts"]["activity_events"]

        engine.dispose()
        forget_engine(url)
        drop_db(name)                                                  # ---- WIPE: the database is gone
        assert not database_exists(name)

        result = restore.restore_backup(info.path, url)                # ---- RESTORE from the dump file alone
        assert result["ok"] and result["mismatches"] == {} and result["revision"] == info.alembic_revision
        assert database_exists(name)

        restored = database.get_engine(url)
        after = everything(restored, settings, sample)
        for key in before:                                             # every report, the dashboard, the status, every table count
            assert after[key] == before[key], f"{key} differs after restore"

    def test_the_app_runs_on_the_restored_data_and_history_protection_survived(self, stack, tmp_path):
        engine = stack.scratch("drill2")
        url, name = stack.urls["scratch-drill2"], stack.names["scratch-drill2"]
        data = build_dataset(engine)
        info = backup.create_backup(url, str(tmp_path))
        with engine.connect() as c:
            top = c.execute(text("SELECT max(venue_seq) FROM activity_events WHERE venue_id = 'college'")).scalar_one()
        engine.dispose()
        forget_engine(url)
        drop_db(name)
        restore.restore_backup(info.path, url)

        app = create_app(Settings(mode="venue", venue_id="hall", database_url=url))
        client = new_client(app)
        assert client.get("/health").json()["db"] == "up"
        assert api_login(client, "admin").status_code == 200           # the accounts came back too
        body = client.get("/admin/api/reports/school-summary").json()
        assert body["totals"]["registered"] == 62 and body["totals"]["reported"] == 17    # the previous bundle's known answers
        assert client.get("/admin/api/dashboard").json()["counts"]["not_attended"] == 4 + 40  # 4 in the dataset + 40 seeded, never registered

        restored = database.get_engine(url)
        for sql in ("UPDATE activity_events SET flags = flags", "DELETE FROM activity_events", "TRUNCATE audit_log"):
            with restored.connect() as c:                              # triggers are part of the schema: they came back
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql))
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION, sql
        with restored.begin() as c:                                    # the gap-free counter carries on where it stopped
            c.execute(text("INSERT INTO activity_events (student_id, activity, kind, venue_id, station_id, operator_id) "
                           "VALUES (:s, 'REGISTRATION', 'COMPLETE', 'college', 'SEED', gen_random_uuid())"), {"s": data.people["A1"].s.id})
            assert c.execute(text("SELECT max(venue_seq) FROM activity_events WHERE venue_id = 'college'")).scalar_one() == top + 1

    def test_a_restore_never_overwrites_data_by_accident_and_refuses_a_damaged_backup(self, stack, tmp_path):
        engine = stack.scratch("drill3")
        url = stack.urls["scratch-drill3"]
        build_dataset(engine)
        info = backup.create_backup(url, str(tmp_path))
        keep = everything_counts(engine)
        with pytest.raises(restore.RestoreError) as exc:               # the database already holds data: nothing happens
            restore.restore_backup(info.path, url)
        assert "already holds data" in str(exc.value) and everything_counts(engine) == keep

        damaged = pathlib.Path(info.path)
        raw = bytearray(damaged.read_bytes())
        raw[len(raw) // 2] ^= 0xFF                                     # one flipped byte in the middle of the dump
        damaged.write_bytes(bytes(raw))
        assert backup.verify_backup(info.path)["ok"] is False
        with pytest.raises(restore.RestoreError) as exc:
            restore.restore_backup(info.path, url, replace=True)
        assert "cannot be trusted" in str(exc.value) and "SHA-256" in str(exc.value)
        assert everything_counts(engine) == keep                       # and the good database was not touched

        pathlib.Path(info.manifest_path).unlink()
        assert backup.verify_backup(info.path)["ok"] is False           # no manifest: cannot be verified, so cannot be trusted

    def test_replace_overwrites_a_database_on_purpose_and_a_missing_database_is_created(self, stack, tmp_path):
        engine = stack.scratch("drill4")
        url, name = stack.urls["scratch-drill4"], stack.names["scratch-drill4"]
        build_dataset(engine)
        info = backup.create_backup(url, str(tmp_path))
        with engine.begin() as c:                                       # changes made after the backup ...
            c.execute(text("INSERT INTO exceptions (type) VALUES ('AFTER_BACKUP')"))
        engine.dispose()
        forget_engine(url)
        result = restore.restore_backup(info.path, url, replace=True)   # ... are gone when the backup is restored over it
        assert result["ok"]
        with database.get_engine(url).connect() as c:
            assert c.execute(text("SELECT count(*) FROM exceptions WHERE type = 'AFTER_BACKUP'")).scalar_one() == 0
        forget_engine(url)
        drop_db(name)
        assert restore.restore_backup(info.path, url)["ok"]              # a clean machine: the database does not exist yet


def everything_counts(engine):
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in ("students", "activity_events", "audit_log", "exceptions")}


# =========================================================================== REBUILD CENTRAL
class TestRebuildCentral:
    def _populate_through_real_sync(self, stack):
        stack.start_central()
        college = stack.operator("college", "REGISTRATION", "REG-01")
        register(stack, college, range(1, 16))
        for _ in range(2):
            for v in ("college", "stadium", "hall"):
                assert stack.worker(v).cycle().ok
        allocation = stack.operator("stadium", "THOBE_ALLOCATION", "THO-01")
        for i in range(1, 11):
            assert stack.confirm(allocation, "THO-01", i)["result"] == "CONFIRMED"
        first = stack.scalar("stadium", "SELECT event_id::text FROM activity_events WHERE activity = 'THOBE_ALLOCATION' ORDER BY venue_seq LIMIT 1")
        assert stack.admin("stadium").post("/admin/api/corrections/reverse", json={"event_id": first, "reason": "wrong student"}).status_code == 200
        for _ in range(2):
            for v in ("college", "stadium", "hall"):
                assert stack.worker(v).cycle().ok
        register(stack, college, [16])                                   # registered at the College, NEVER sent to central
        return first

    def test_wipe_central_and_rebuild_it_from_the_venues(self, stack):
        self._populate_through_real_sync(stack)
        central_before = {"events": stack.events("central"), "status": status_rows(stack.engine("central")),
                          "counts": stack.admin("central").get("/admin/api/dashboard").json()["counts"]}
        assert len(central_before["events"]) == 15 + 10 + 1 and stack.scalar("central", "SELECT count(*) FROM activity_events WHERE student_id = :s", s=stack.student_id(16)) == 0
        old_epoch = stack.scalar("central", "SELECT value FROM sync_meta WHERE key = 'epoch'")
        stack.stop_central()

        stack.wipe_to_empty("central")                                   # ---- CENTRAL IS LOST: a brand-new empty database
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 0 and stack.scalar("central", "SELECT count(*) FROM students") == 0

        report = rebuild_central(stack.engine("central"), {v: stack.engine(v) for v in ("college", "stadium", "hall")}, master_from="stadium")
        assert report["ok"] and report["started_empty"] and report["master_copied"]["students"] == 40
        assert all(v["ok"] and v["read_from_venue"] == v["now_at_central"] for v in report["venues"].values())
        assert report["new_epoch"] != old_epoch                          # so the venues restart their cursors

        # central now holds EXACTLY the union of what the venues authored (including the event that never reached it before)
        authored = sorted((e for v in ("college", "stadium", "hall") for e in stack.events(v, f"venue_id = '{v}'")), key=lambda e: (e["venue_id"], e["venue_seq"]))
        rebuilt = stack.events("central")
        assert rebuilt == authored and len(rebuilt) == 15 + 10 + 1 + 1
        assert stack.scalar("central", "SELECT count(*) FROM activity_events WHERE student_id = :s", s=stack.student_id(16)) == 1
        assert stack.scalar("central", "SELECT count(*) FROM sync_log") == len(rebuilt)
        # derived status is identical to before for every student the old central knew about; only student 16 changed
        before_status = dict((s[0], s[1:]) for s in central_before["status"])
        for row in status_rows(stack.engine("central")):
            if row[0] != str(stack.student_id(16)):
                assert row[1:] == before_status[row[0]], row
        with stack.engine("central").begin() as conn:                    # keys are stored hashed, so a new central needs new keys,
            for v in ("college", "stadium", "hall"):                     # and user accounts are not copied: the Admin is re-seeded
                stack.keys[v] = issue_key(conn, v, "after rebuild")
            users_svc.create_user(conn, username="admin", password=PASSWORD, role="ADMIN")
        stack._apps.pop("central", None)

        # the venues carry on: their pull cursors restart (new epoch) and nothing is applied twice
        stack.start_central()
        stadium_events = stack.scalar("stadium", "SELECT count(*) FROM activity_events")
        for v in ("college", "stadium", "hall"):
            assert stack.worker(v).cycle().ok
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events") == stadium_events + 1   # only student 16 was new (it came from the College)
        assert stack.scalar("stadium", "SELECT epoch FROM sync_state WHERE peer = 'central'") == report["new_epoch"]
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == stack.scalar("central", "SELECT count(DISTINCT event_id) FROM activity_events")
        assert unsent_count(stack, "college") == 0                       # the College's queue for student 16 is delivered as DUPLICATE, not duplicated
        # and NEW events flow again
        register(stack, stack.operator("college", "REGISTRATION", "REG-01"), [17])
        assert stack.worker("college").cycle().ok and stack.worker("stadium").cycle().ok
        assert stack.scalar("stadium", "SELECT count(*) FROM activity_events WHERE student_id = :s", s=stack.student_id(17)) == 1
        assert stack.admin("central").get("/admin/api/dashboard").json()["counts"]["reported"] == 17    # students 1-15, 16 (rebuilt in) and 17 (after)

    def test_rebuilding_twice_or_onto_a_partly_rebuilt_central_adds_nothing(self, stack):
        self._populate_through_real_sync(stack)
        stack.stop_central()
        venues = {v: stack.engine(v) for v in ("college", "stadium", "hall")}
        first = rebuild_central(stack.engine("central"), venues)          # onto a central that is already full
        assert first["ok"] and all(v["accepted"] <= 1 for v in first["venues"].values())   # only the one unsynced event is new
        events = stack.events("central")
        second = rebuild_central(stack.engine("central"), venues)
        assert second["ok"] and all(v["accepted"] == 0 for v in second["venues"].values()) and stack.events("central") == events

    def test_a_rebuild_reports_a_mismatch_instead_of_pretending(self, stack):
        self._populate_through_real_sync(stack)
        stack.stop_central()
        stack.wipe_to_empty("central")                                    # central has NO students: every event will be refused
        report = rebuild_central(stack.engine("central"), {v: stack.engine(v) for v in ("college", "stadium", "hall")})
        assert report["ok"] is False and any(not v["ok"] for v in report["venues"].values())
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 0

    def test_the_rebuild_command_line_works_end_to_end(self, stack):
        self._populate_through_real_sync(stack)
        stack.stop_central()
        stack.wipe_to_empty("central")
        args = [sys.executable, "-m", "backend.sync.rebuild", "--central-url", stack.urls["central"], "--master-from", "stadium"]
        for v in ("college", "stadium", "hall"):
            args += ["--venue", f"{v}={stack.urls[v]}"]
        done = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "REBUILD VERIFIED" in done.stdout and "college" in done.stdout
        assert stack.scalar("central", "SELECT count(*) FROM activity_events") == 27


def unsent_count(stack, venue):
    return stack.scalar(venue, "SELECT count(*) FROM outbox WHERE sent_at IS NULL AND rejected_at IS NULL")


# =========================================================================== THE BACKUP SCHEDULE
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TestBackupSchedule:
    def test_the_documented_interval_is_five_minutes(self):
        assert backup.DEFAULT_INTERVAL_SECONDS == 300 == Settings(mode="central").backup_interval_seconds
        compose = (pathlib.Path(REPO_ROOT) / "docker-compose.yml").read_text(encoding="utf-8")
        assert "backend.ha.backup" in compose and "BACKUP_INTERVAL_SECONDS:-300" in compose
        assert compose.count("restart: unless-stopped") >= 3                # db, app and backup all come back after a crash or reboot

    def test_a_backup_is_taken_every_interval_and_time_is_fast_forwarded_not_waited_for(self):
        clock, taken = FakeClock(), []
        scheduler = backup.BackupScheduler(lambda: taken.append(clock()) or backup.BackupInfo(
            "n", "p", "m", "auto", None, "t", 1, "s", None, None, {}), interval=300, clock=clock)
        for second in range(0, 3601, 30):                                    # one simulated hour, ticking every 30 seconds
            clock.t = second
            scheduler.tick()
        assert taken == [float(t) for t in range(0, 3601, 300)]              # at 0, 5, 10 ... 60 minutes: 13 backups, exactly on the interval

    def test_intervals_missed_while_the_machine_was_busy_are_skipped_not_replayed(self):
        clock, taken = FakeClock(), []
        scheduler = backup.BackupScheduler(lambda: taken.append(clock()) or backup.BackupInfo("n", "p", "m", "auto", None, "t", 1, "s", None, None, {}),
                                           interval=300, clock=clock)
        scheduler.tick()
        clock.t = 2000                                                       # the laptop was asleep for 33 minutes
        assert scheduler.tick() is not None and scheduler.tick() is None      # ONE catch-up backup, not six
        assert scheduler.next_due == 2100                                    # and back on the 5-minute grid
        assert len(taken) == 2

    def test_a_failed_backup_is_retried_soon_and_does_not_stop_the_schedule(self):
        clock, outcomes = FakeClock(), [RuntimeError("disk full"), RuntimeError("disk full"), "ok"]
        def run():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return backup.BackupInfo("n", "p", "m", "auto", None, "t", 1, "s", None, None, {})
        scheduler = backup.BackupScheduler(run, interval=300, clock=clock)
        assert scheduler.tick() is None and scheduler.failures == 1 and scheduler.last_error == "disk full"
        clock.t = 29
        assert scheduler.tick() is None and scheduler.failures == 1            # not yet: the retry is 30 s away, not 5 minutes
        clock.t = 30
        assert scheduler.tick() is None and scheduler.failures == 2
        clock.t = 60
        assert scheduler.tick() is not None and scheduler.last_error is None    # third time lucky

    def test_real_dumps_on_a_fast_forwarded_schedule_with_retention_that_never_removes_a_milestone(self, stack, tmp_path):
        engine = stack.scratch("sched")
        url = stack.urls["scratch-sched"]
        build_dataset(engine)
        target = str(tmp_path / "second-device")
        milestone = backup.create_backup(url, target, kind="milestone", label="before-event")
        clock = FakeClock()
        scheduler = backup.BackupScheduler(lambda: backup.create_backup(url, target), 300, clock=clock,
                                           after_success=lambda info: backup.prune(target, keep=3))
        made = []
        for minute in range(0, 31):                                           # thirty simulated minutes, ticking every minute
            clock.t = minute * 60
            info = scheduler.tick()
            made.append(info)
            if minute == 12:
                with engine.begin() as c:                                      # the data changes between backups
                    c.execute(text("INSERT INTO exceptions (type) VALUES ('MID_EVENT')"))
        taken = [m for m in made if m]
        assert len(taken) == 7                                                # 0, 5, 10, 15, 20, 25, 30 minutes
        listing = backup.list_backups(target)
        autos = [b for b in listing if b["kind"] == "auto"]
        assert len(autos) == 3 and [a["name"] for a in autos] == [t.name for t in reversed(taken[-3:])]   # only the newest three
        assert any(b["kind"] == "milestone" and b["name"] == milestone.name for b in listing)          # the milestone was never touched
        assert pathlib.Path(milestone.path).exists()
        assert taken[-1].counts["exceptions"] == taken[0].counts["exceptions"] + 1                      # manifests record each moment's counts
        assert backup.latest_manifest(target)["name"] == taken[-1].name
        assert all(backup.verify_backup(b["path"])["ok"] for b in listing)
        assert not list(pathlib.Path(target).glob("*.partial"))                # no half-written files left behind

    def test_the_admin_dashboard_shows_when_the_last_backup_was_taken(self, stack, tmp_path):
        engine = stack.scratch("dashb")
        url = stack.urls["scratch-dashb"]
        settings = Settings(mode="venue", venue_id="hall", database_url=url, backup_dir=str(tmp_path))
        with engine.connect() as c:
            assert dashboard.venue_health(c, settings)["last_backup_at"] is None
        info = backup.create_backup(url, str(tmp_path))
        with engine.connect() as c:
            assert dashboard.venue_health(c, settings)["last_backup_at"] == info.created_at

    def test_the_backup_and_restore_commands_work_from_the_command_line(self, stack, tmp_path):
        engine = stack.scratch("cli")
        url, name = stack.urls["scratch-cli"], stack.names["scratch-cli"]
        build_dataset(engine)
        env = {**os.environ, "DATABASE_URL": url, "BACKUP_DIR": str(tmp_path)}
        run = lambda *a: subprocess.run([sys.executable, "-m", *a], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180)  # noqa: E731
        once = run("backend.ha.backup", "once")
        milestone = run("backend.ha.backup", "milestone", "--label", "after-ceremony")
        assert once.returncode == 0 and milestone.returncode == 0, once.stdout + once.stderr + milestone.stdout + milestone.stderr
        listing = run("backend.ha.backup", "list")
        assert len(listing.stdout.strip().splitlines()) == 2 and "milestone" in listing.stdout and "after-ceremony" in listing.stdout
        dump = next(pathlib.Path(tmp_path).glob("*-auto.dump"))
        assert run("backend.ha.backup", "verify", str(dump)).stdout.strip() == "OK"
        refused = run("backend.ha.restore", "--latest-from", str(tmp_path))                 # onto a database that has data
        assert refused.returncode == 1 and "already holds data" in refused.stdout
        other = url_for(name + "_clean")                                                    # "a clean laptop": a database that is not there yet
        done = subprocess.run([sys.executable, "-m", "backend.ha.restore", "--latest-from", str(tmp_path), "--database-url", other],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
        try:
            assert done.returncode == 0 and "RESTORED AND VERIFIED" in done.stdout, done.stdout + done.stderr
        finally:
            drop_db(name + "_clean")


# =========================================================================== A BACKUP THAT CAPTURED NOTHING
class TestABackupNeverSucceedsQuietlyOnAnUnusableDatabase:
    """SYSTEM_SPEC 21. A dump of a database whose migrations never ran is a file that LOOKS like a backup
    and restores nothing. `pg_dump` produces one happily, and `pg_restore --list` reads it back happily, so
    neither of the existing checks notices. The job must refuse it, say so at ERROR level and exit non-zero,
    and must never write the "backup written" line for a dump that captured no rows.
    """

    def _run_cli(self, *args, url, dest, timeout=90):
        env = {**os.environ, "DATABASE_URL": url, "BACKUP_DIR": str(dest)}
        return subprocess.run([sys.executable, "-m", "backend.ha.backup", *args], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=timeout)

    def test_a_schema_less_database_is_refused_and_leaves_no_file_behind(self, bare_database, tmp_path, caplog):
        dest = tmp_path / "second-device"
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            with pytest.raises(backup.BackupError) as raised:
                backup.create_backup(bare_database, str(dest))
        assert "alembic upgrade head" in str(raised.value)
        assert "does not exist" in str(raised.value)
        assert "backup written" not in caplog.text                     # never an INFO success line
        assert backup.list_backups(str(dest)) == []
        assert list(dest.glob("*")) == []                              # no dump, no manifest, no .partial

    def test_a_migrated_but_completely_empty_database_captured_zero_rows_and_is_refused(self, bare_database, tmp_path, caplog):
        assert run_alembic("upgrade", "head", database_url=bare_database).returncode == 0
        dest = tmp_path / "second-device"
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            with pytest.raises(backup.BackupError, match="zero rows"):
                backup.create_backup(bare_database, str(dest))
        assert "backup written" not in caplog.text
        assert list(dest.glob("*")) == []

    def test_a_missing_table_is_an_error_even_though_pg_dump_itself_succeeds(self, stack, tmp_path, caplog):
        engine = stack.scratch("broken")
        url = stack.urls["scratch-broken"]
        build_dataset(engine)
        dest = tmp_path / "second-device"

        good = backup.create_backup(url, str(dest))                    # the same database, while its schema is whole
        assert good.counts["activity_events"] > 0

        with engine.begin() as c:
            c.execute(text("DROP TABLE conflict_events CASCADE"))      # what a half-applied migration leaves behind
        caplog.clear()                                                 # from here on, only the broken run's log
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            with pytest.raises(backup.BackupError) as raised:
                backup.create_backup(url, str(dest))
        assert "conflict_events" in str(raised.value) and "does not exist" in str(raised.value)
        assert "backup written" not in caplog.text
        assert [b["name"] for b in backup.list_backups(str(dest))] == [good.name]   # only the good one is there
        assert not list(dest.glob("*.partial"))

    def test_the_command_line_exits_non_zero_and_logs_an_error_without_a_success_line(self, bare_database, tmp_path):
        dest = tmp_path / "second-device"
        done = self._run_cli("once", url=bare_database, dest=dest)
        output = done.stdout + done.stderr
        assert done.returncode != 0, output
        assert "[ERROR]" in output, output
        assert "written:" not in output and "backup written" not in output, output
        assert not dest.exists() or list(dest.glob("*")) == []

    def test_the_backup_job_refuses_to_start_against_a_schema_less_database(self, bare_database, tmp_path):
        """`backend.ha.backup run` is what docker-compose starts. It must not sit in a retry loop
        logging success against a database that has no schema."""
        dest = tmp_path / "second-device"
        try:
            done = self._run_cli("run", url=bare_database, dest=dest, timeout=45)
        except subprocess.TimeoutExpired:
            pytest.fail("the backup job kept running against a schema-less database instead of failing loudly")
        output = done.stdout + done.stderr
        assert done.returncode != 0, output
        assert "[ERROR]" in output, output
        assert "backup written" not in output, output
        assert not dest.exists() or list(dest.glob("*")) == []

    def test_a_real_backup_of_a_real_database_still_succeeds_and_says_so(self, stack, tmp_path, caplog):
        engine = stack.scratch("healthy")
        build_dataset(engine)
        dest = tmp_path / "second-device"
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            info = backup.create_backup(stack.urls["scratch-healthy"], str(dest))
        assert "backup written" in caplog.text
        assert info.counts["students"] > 0 and info.alembic_revision
        assert backup.verify_backup(info.path)["ok"]

# =========================================================================== FAILOVER (documented best effort)
class TestFailoverMaterials:
    def test_the_failover_script_parses_and_lays_out_every_step_without_touching_anything(self):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is needed to check the failover script")
        script = str(pathlib.Path(REPO_ROOT) / "scripts" / "failover.sh")
        assert subprocess.run([bash, "-n", script], capture_output=True, text=True).returncode == 0     # valid shell
        done = subprocess.run([bash, script, "--venue", "stadium", "--dry-run"], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        assert done.returncode == 0, done.stdout + done.stderr
        for step in ("Check that the old server is really down", "Promote the standby database", "Start the application",
                     "Take over the server's address", "Check the server is healthy", "DRY RUN"):
            assert step in done.stdout, step
        refused = subprocess.run([bash, script, "--dry-run"], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)   # no venue named
        assert refused.returncode != 0

    @pytest.mark.parametrize("venue,address", [("COLLEGE", "college.local"), ("STADIUM", "stadium.local"), ("HALL", "hall.local")])
    def test_each_venue_has_a_one_page_plain_language_procedure(self, venue, address):
        page = (FAILOVER_DIR / f"{venue}.md").read_text(encoding="utf-8")
        lines = [l for l in page.splitlines() if l.strip()]
        assert len(lines) <= 70                                              # one page
        assert address in page and "failover.sh" in page and "2 minutes" in page
        assert len(re.findall(r"^\d+\. ", page, re.M)) >= 6                  # numbered steps a person can follow under pressure
        assert re.search(r"call|phone|ring", page, re.I)                     # and who to call when it does not work
        for jargon in ("systemctl", "pg_ctl", "WAL", "replication slot", "iptables"):
            assert jargon not in page, jargon                                # commands live in the script, not on the sheet

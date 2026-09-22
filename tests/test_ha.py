"""Phase 17: high availability. Backups, restore, and the automated schedule.

What is proven here, on the real PostgreSQL tools (`pg_dump` / `pg_restore`) and real separate databases:

  * RESTORE DRILL       A populated database is dumped, WIPED (dropped), restored from the dump alone, and every report
                        from the previous bundle regenerates IDENTICALLY; the app then runs on the restored data, the
                        append-only triggers are still in force, and the gap-free counters carry on.
  * BACKUP SCHEDULE     Every 5 minutes (documented default), fast-forwarded with a fake clock rather than waited for.
                        Retention never removes a milestone; a failed run is retried; missed intervals are not replayed.
  * FAILOVER MATERIALS  Single standby server failover procedure (docs/failover/SERVER.md) and script (scripts/failover.sh).

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
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend import database
from backend import users as users_svc
from backend.admin import dashboard, reports
from backend.config import Settings
from backend.ha import backup, pgtools, restore
from backend.main import create_app
from tests.admin_support import build_dataset
from tests.conftest import TEST_DB_URL, run_alembic
from tests.test_auth import PASSWORD, RESTRICT_VIOLATION, api_login, new_client
from tests.test_schema import REPO_ROOT

FAILOVER_DIR = pathlib.Path(REPO_ROOT) / "docs" / "failover"


def url_for(name: str) -> str:
    url = make_url(TEST_DB_URL)
    return str(url.set(database=name).render_as_string(hide_password=False))


def create_db(name: str) -> str:
    url = url_for(name)
    admin_url = make_url(TEST_DB_URL).set(database="postgres" if make_url(TEST_DB_URL).database != "postgres" else "template1")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    run_alembic("upgrade", "head", database_url=url)
    return url


def drop_db(name: str):
    admin_url = make_url(TEST_DB_URL).set(database="postgres" if make_url(TEST_DB_URL).database != "postgres" else "template1")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" (FORCE)'))
    finally:
        admin.dispose()


def forget_engine(url: str) -> None:
    eng = database._engines.pop(url, None)
    if eng is not None:
        eng.dispose()
    database._sessionmakers.pop(url, None)


def database_exists(name: str) -> bool:
    admin_url = make_url(TEST_DB_URL).set(database="postgres" if make_url(TEST_DB_URL).database != "postgres" else "template1")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            return bool(c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar())
    finally:
        admin.dispose()


def status_rows(engine) -> list[tuple]:
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT student_id::text, step, status FROM student_status ORDER BY student_id"))]


class ScratchDatabases:
    def __init__(self, tag="ha"):
        self.tag = tag
        self.created = []

    def make(self, name):
        full_name = f"convocation_test_{self.tag}_{name}"
        url = create_db(full_name)
        self.created.append(full_name)
        eng = database.get_engine(url)
        return eng, url, full_name

    def close(self):
        for name in self.created:
            try:
                forget_engine(url_for(name))
            except Exception:
                pass
            try:
                drop_db(name)
            except Exception:
                pass


@pytest.fixture(scope="module")
def scratches():
    s = ScratchDatabases("ha")
    yield s
    s.close()


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
    snap.pop("as_of")
    out["dashboard"] = json.loads(json.dumps(snap, default=str))
    out["status"] = status_rows(engine)
    with engine.connect() as conn:
        out["table_counts"] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in
                               ("students", "activity_events", "audit_log", "scan_log", "exceptions", "queue", "qr_tokens")}
    return out


def everything_counts(engine):
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in ("students", "activity_events", "audit_log", "exceptions")}


# =========================================================================== THE RESTORE DRILL
class TestRestoreDrill:
    def test_dump_wipe_restore_and_every_report_regenerates_identically(self, scratches, tmp_path):
        engine, url, name = scratches.make("drill")
        data = build_dataset(engine)
        settings = Settings(database_url=url)
        sample = data.people["L"].s.id
        before = everything(engine, settings, sample)
        assert before["table_counts"]["activity_events"] > 20 and len(before["status"]) > 0
        assert set(reports.REPORTS) <= set(before)

        info = backup.create_backup(url, str(tmp_path), kind="milestone", label="before-event")
        assert pathlib.Path(info.path).stat().st_size > 10_000 and info.counts["activity_events"] == before["table_counts"]["activity_events"]

        engine.dispose()
        forget_engine(url)
        drop_db(name)
        assert not database_exists(name)

        result = restore.restore_backup(info.path, url)
        assert result["ok"] and result["mismatches"] == {} and result["revision"] == info.alembic_revision
        assert database_exists(name)

        restored = database.get_engine(url)
        after = everything(restored, settings, sample)
        for key in before:
            assert after[key] == before[key], f"{key} differs after restore"

    def test_the_app_runs_on_the_restored_data_and_history_protection_survived(self, scratches, tmp_path):
        engine, url, name = scratches.make("drill2")
        data = build_dataset(engine)
        with engine.begin() as c:
            users_svc.create_user(c, username="admin", password=PASSWORD, role="ADMIN")
        info = backup.create_backup(url, str(tmp_path))
        with engine.connect() as c:
            top = c.execute(text("SELECT count(*) FROM activity_events")).scalar_one()
        engine.dispose()
        forget_engine(url)
        drop_db(name)
        restore.restore_backup(info.path, url)

        app = create_app(Settings(database_url=url))
        client = new_client(app)
        assert client.get("/health").json()["db"] == "up"
        assert api_login(client, "admin").status_code == 200
        body = client.get("/admin/api/reports/school-summary").json()
        assert body["totals"]["registered"] == 22 and body["totals"]["reported"] == 17
        assert client.get("/admin/api/dashboard").json()["counts"]["not_attended"] == 4

        restored = database.get_engine(url)
        for sql in ("UPDATE activity_events SET flags = flags", "DELETE FROM activity_events", "TRUNCATE audit_log"):
            with restored.connect() as c:
                with pytest.raises(DBAPIError) as exc:
                    c.execute(text(sql))
                assert exc.value.orig.pgcode == RESTRICT_VIOLATION, sql
        with restored.begin() as c:
            c.execute(text("INSERT INTO activity_events (student_id, activity, kind, operator_id) "
                           "VALUES (:s, 'REGISTRATION', 'COMPLETE', gen_random_uuid())"), {"s": data.people["A1"].s.id})
            assert c.execute(text("SELECT count(*) FROM activity_events")).scalar_one() == top + 1

    def test_a_restore_never_overwrites_data_by_accident_and_refuses_a_damaged_backup(self, scratches, tmp_path):
        engine, url, name = scratches.make("drill3")
        build_dataset(engine)
        info = backup.create_backup(url, str(tmp_path))
        keep = everything_counts(engine)
        with pytest.raises(restore.RestoreError) as exc:
            restore.restore_backup(info.path, url)
        assert "already holds data" in str(exc.value) and everything_counts(engine) == keep

        damaged = pathlib.Path(info.path)
        raw = bytearray(damaged.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        damaged.write_bytes(bytes(raw))
        assert backup.verify_backup(info.path)["ok"] is False
        with pytest.raises(restore.RestoreError) as exc:
            restore.restore_backup(info.path, url, replace=True)
        assert "cannot be trusted" in str(exc.value) and "SHA-256" in str(exc.value)
        assert everything_counts(engine) == keep

        pathlib.Path(info.manifest_path).unlink()
        assert backup.verify_backup(info.path)["ok"] is False

    def test_replace_overwrites_a_database_on_purpose_and_a_missing_database_is_created(self, scratches, tmp_path):
        engine, url, name = scratches.make("drill4")
        build_dataset(engine)
        info = backup.create_backup(url, str(tmp_path))
        with engine.begin() as c:
            c.execute(text("INSERT INTO exceptions (type) VALUES ('AFTER_BACKUP')"))
        engine.dispose()
        forget_engine(url)
        result = restore.restore_backup(info.path, url, replace=True)
        assert result["ok"]
        with database.get_engine(url).connect() as c:
            assert c.execute(text("SELECT count(*) FROM exceptions WHERE type = 'AFTER_BACKUP'")).scalar_one() == 0
        forget_engine(url)
        drop_db(name)
        assert restore.restore_backup(info.path, url)["ok"]


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
        assert backup.DEFAULT_INTERVAL_SECONDS == 300 == Settings().backup_interval_seconds
        compose = (pathlib.Path(REPO_ROOT) / "docker-compose.yml").read_text(encoding="utf-8")
        assert "backend.ha.backup" in compose and "BACKUP_INTERVAL_SECONDS:-300" in compose
        assert compose.count("restart: unless-stopped") >= 3

    def test_a_backup_is_taken_every_interval_and_time_is_fast_forwarded_not_waited_for(self):
        clock, taken = FakeClock(), []
        scheduler = backup.BackupScheduler(lambda: taken.append(clock()) or backup.BackupInfo(
            "n", "p", "m", "auto", None, "t", 1, "s", None, None, {}), interval=300, clock=clock)
        for second in range(0, 3601, 30):
            clock.t = second
            scheduler.tick()
        assert taken == [float(t) for t in range(0, 3601, 300)]

    def test_intervals_missed_while_the_machine_was_busy_are_skipped_not_replayed(self):
        clock, taken = FakeClock(), []
        scheduler = backup.BackupScheduler(lambda: taken.append(clock()) or backup.BackupInfo("n", "p", "m", "auto", None, "t", 1, "s", None, None, {}),
                                           interval=300, clock=clock)
        scheduler.tick()
        clock.t = 2000
        assert scheduler.tick() is not None and scheduler.tick() is None
        assert scheduler.next_due == 2100
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
        assert scheduler.tick() is None and scheduler.failures == 1
        clock.t = 30
        assert scheduler.tick() is None and scheduler.failures == 2
        clock.t = 60
        assert scheduler.tick() is not None and scheduler.last_error is None

    def test_real_dumps_on_a_fast_forwarded_schedule_with_retention_that_never_removes_a_milestone(self, scratches, tmp_path):
        engine, url, name = scratches.make("sched")
        build_dataset(engine)
        target = str(tmp_path / "second-device")
        milestone = backup.create_backup(url, target, kind="milestone", label="before-event")
        clock = FakeClock()
        scheduler = backup.BackupScheduler(lambda: backup.create_backup(url, target), 300, clock=clock,
                                           after_success=lambda info: backup.prune(target, keep=3))
        made = []
        for minute in range(0, 31):
            clock.t = minute * 60
            info = scheduler.tick()
            made.append(info)
            if minute == 12:
                with engine.begin() as c:
                    c.execute(text("INSERT INTO exceptions (type) VALUES ('MID_EVENT')"))
        taken = [m for m in made if m]
        assert len(taken) == 7
        listing = backup.list_backups(target)
        autos = [b for b in listing if b["kind"] == "auto"]
        assert len(autos) == 3 and [a["name"] for a in autos] == [t.name for t in reversed(taken[-3:])]
        assert any(b["kind"] == "milestone" and b["name"] == milestone.name for b in listing)
        assert pathlib.Path(milestone.path).exists()
        assert taken[-1].counts["exceptions"] == taken[0].counts["exceptions"] + 1
        assert backup.latest_manifest(target)["name"] == taken[-1].name
        assert all(backup.verify_backup(b["path"])["ok"] for b in listing)
        assert not list(pathlib.Path(target).glob("*.partial"))

    def test_the_admin_dashboard_shows_when_the_last_backup_was_taken(self, scratches, tmp_path):
        engine, url, name = scratches.make("dashb")
        build_dataset(engine)
        settings = Settings(database_url=url, backup_dir=str(tmp_path))
        with engine.connect() as c:
            assert dashboard.server_health(c, settings)["last_backup_at"] is None
        info = backup.create_backup(url, str(tmp_path))
        with engine.connect() as c:
            assert dashboard.server_health(c, settings)["last_backup_at"] == info.created_at

    def test_the_backup_and_restore_commands_work_from_the_command_line(self, scratches, tmp_path):
        engine, url, name = scratches.make("cli")
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
        refused = run("backend.ha.restore", "--latest-from", str(tmp_path))
        assert refused.returncode == 1 and "already holds data" in refused.stdout
        other_name = name + "_clean"
        other = url_for(other_name)
        done = subprocess.run([sys.executable, "-m", "backend.ha.restore", "--latest-from", str(tmp_path), "--database-url", other],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
        try:
            assert done.returncode == 0 and "RESTORED AND VERIFIED" in done.stdout, done.stdout + done.stderr
        finally:
            drop_db(other_name)


# =========================================================================== A BACKUP THAT CAPTURED NOTHING
class TestABackupNeverSucceedsQuietlyOnAnUnusableDatabase:
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
        assert "backup written" not in caplog.text
        assert backup.list_backups(str(dest)) == []
        assert list(dest.glob("*")) == []

    def test_a_migrated_but_completely_empty_database_captured_zero_rows_and_is_refused(self, bare_database, tmp_path, caplog):
        assert run_alembic("upgrade", "head", database_url=bare_database).returncode == 0
        dest = tmp_path / "second-device"
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            with pytest.raises(backup.BackupError, match="zero rows"):
                backup.create_backup(bare_database, str(dest))
        assert "backup written" not in caplog.text
        assert list(dest.glob("*")) == []

    def test_a_missing_table_is_an_error_even_though_pg_dump_itself_succeeds(self, scratches, tmp_path, caplog):
        engine, url, name = scratches.make("broken")
        build_dataset(engine)
        dest = tmp_path / "second-device"

        good = backup.create_backup(url, str(dest))
        assert good.counts["activity_events"] > 0

        with engine.begin() as c:
            c.execute(text("DROP TABLE audit_log CASCADE"))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            with pytest.raises(backup.BackupError) as raised:
                backup.create_backup(url, str(dest))
        assert "audit_log" in str(raised.value) and "does not exist" in str(raised.value)
        assert "backup written" not in caplog.text
        assert [b["name"] for b in backup.list_backups(str(dest))] == [good.name]
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

    def test_a_real_backup_of_a_real_database_still_succeeds_and_says_so(self, scratches, tmp_path, caplog):
        engine, url, name = scratches.make("healthy")
        build_dataset(engine)
        dest = tmp_path / "second-device"
        with caplog.at_level(logging.INFO, logger="backend.ha"):
            info = backup.create_backup(url, str(dest))
        assert "backup written" in caplog.text
        assert info.counts["students"] > 0 and info.alembic_revision
        assert backup.verify_backup(info.path)["ok"]


def find_bash():
    for candidate in [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        shutil.which("bash"),
    ]:
        if candidate and pathlib.Path(candidate).exists():
            try:
                res = subprocess.run([candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip() == "ok":
                    return candidate
            except Exception:
                continue
    return None


# =========================================================================== FAILOVER (single server)
class TestFailoverMaterials:
    def test_the_failover_script_parses_and_lays_out_every_step_without_touching_anything(self):
        bash = find_bash()
        if bash is None:
            pytest.skip("bash is needed to check the failover script")
        script = str(pathlib.Path(REPO_ROOT) / "scripts" / "failover.sh")
        assert subprocess.run([bash, "-n", script], capture_output=True, text=True).returncode == 0
        done = subprocess.run([bash, script, "--dry-run"], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
        assert done.returncode == 0, done.stdout + done.stderr
        for step in ("Check that the old server is really down", "Promote the standby database", "Start the application",
                     "Take over the server's address", "Check the server is healthy", "DRY RUN"):
            assert step in done.stdout, step

    def test_single_server_has_a_one_page_plain_language_procedure(self):
        page = (FAILOVER_DIR / "SERVER.md").read_text(encoding="utf-8")
        lines = [l for l in page.splitlines() if l.strip()]
        assert len(lines) <= 70
        assert "convocation.local" in page and "failover.sh" in page and "2 minutes" in page
        assert len(re.findall(r"^\d+\. ", page, re.M)) >= 6
        assert re.search(r"call|phone|ring", page, re.I)
        for jargon in ("systemctl", "pg_ctl", "WAL", "replication slot", "iptables"):
            assert jargon not in page, jargon

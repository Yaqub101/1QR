"""Backups (SYSTEM_SPEC 21): a full dump on a fixed interval to a SECOND device, milestone backups kept for good.

    python -m backend.ha.backup run                       # every BACKUP_INTERVAL_SECONDS (default 300 = 5 minutes)
    python -m backend.ha.backup once                      # one dump now
    python -m backend.ha.backup milestone --label before-event      # also: after-registration-closes, after-ceremony
    python -m backend.ha.backup list | verify <dump-file>

Each backup is a PostgreSQL custom-format dump (`pg_dump -Fc`) plus a small JSON manifest beside it (time, kind, size,
SHA-256, database revision, and the row counts that matter). A backup is written to a `.partial` file, checked
with `pg_restore --list`, and only then renamed into place, so a crash or a full disk never leaves a file that looks
like a good backup and is not. A failed run is logged and retried on the next tick; the app is never involved, so a
backup can never slow a scan.

Neither of those two checks can tell a good dump from a dump of a database whose migrations never ran: that is a
perfectly valid, perfectly empty archive of about a kilobyte, and `pg_restore --list` reads it back happily. So the
row counts and the file size are checked as well, and a run that captured nothing is an ERROR and a non-zero exit,
never an INFO "backup written" line. `run` refuses to start at all against such a database, rather than logging a
retry every thirty seconds for the rest of the day.

Retention: the newest `keep` AUTOMATIC dumps are kept; a MILESTONE dump is never removed by this job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pathlib
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import create_engine, text

from backend.ha import pgtools

logger = logging.getLogger("backend.ha")

DEFAULT_INTERVAL_SECONDS = 300      # SYSTEM_SPEC 21: "Full database dump: every 5 minutes"
DEFAULT_KEEP = 24                   # two hours of 5-minute dumps
RETRY_AFTER_FAILURE_SECONDS = 30
MILESTONES = ("before-event", "after-registration-closes", "after-ceremony")
# The tables whose row counts go in the manifest, and which together answer "is there anything here worth
# protecting?". `users` is among them because a server that has been set up but has not had its students
# imported yet still holds the accounts, whose passwords nobody can read back out to retype.
COUNTED_TABLES = ("activity_events", "audit_log", "students", "users", "exceptions", "scan_log",
                  "queue", "qr_tokens")
# A dump of a database whose migrations never ran is a real, valid, EMPTY archive of about a kilobyte, which
# `pg_restore --list` reads back perfectly happily. Neither existing check can tell it from a good backup, so
# the size is checked too. Any genuine dump of this schema is far larger than this, data or no data.
MIN_PLAUSIBLE_DUMP_BYTES = 4096
SCHEMA_HINT = "run `alembic upgrade head` on this database"


class BackupError(RuntimeError):
    pass


@dataclass
class BackupInfo:
    name: str
    path: str
    manifest_path: str
    kind: str                 # "auto" | "milestone"
    label: Optional[str]
    created_at: str
    size_bytes: int
    sha256: str
    alembic_revision: Optional[str]
    server_version: Optional[str]
    counts: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short(exc: Exception) -> str:
    """The database's own first line, without the SQLAlchemy essay around it."""
    first = " ".join(str(exc).split())
    for cut in (" LINE ", " [SQL:", " (Background"):
        first = first.split(cut)[0]
    return first[:160]


@dataclass
class Facts:
    revision: Optional[str]
    server_version: Optional[str]
    counts: dict
    problems: list              # tables (or alembic_version) this database could not answer for


def _snapshot_facts(database_url: str) -> Facts:
    """What the manifest records, and - just as important - what could NOT be read. A table that is
    missing is a schema fault, not a table with nothing in it, and the two must never look alike."""
    engine = create_engine(database_url)
    problems: list = []
    try:
        with engine.connect() as conn:
            revision = None
            try:
                revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception as exc:
                conn.rollback()
                problems.append(("alembic_version", _short(exc)))
            counts = {}
            for table in COUNTED_TABLES:
                try:
                    counts[table] = int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
                except Exception as exc:
                    conn.rollback()
                    problems.append((table, _short(exc)))
            return Facts(revision, str(conn.execute(text("SHOW server_version")).scalar()), counts, problems)
    finally:
        engine.dispose()


def check_ready(database_url: str) -> list:
    """Everything that makes a backup of this database worthless, as plain sentences. Empty means the
    database is worth backing up. Used before the backup job starts; create_backup uses `_problems`
    directly, on the facts it has already read."""
    try:
        return _problems(_snapshot_facts(database_url))
    except Exception as exc:
        return [f"the database could not be read: {_short(exc)}"]


def _problems(facts: "Facts") -> list:
    problems = []
    if facts.problems:
        # One sentence, not one paragraph per table: the same error repeated eight times tells an
        # operator nothing the list of table names does not.
        names = ", ".join(name for name, _ in facts.problems)
        example = facts.problems[0][1]
        problems.append(f"the schema is missing or incomplete ({SCHEMA_HINT}) - "
                        f"{len(facts.problems)} table(s) could not be read: {names} [{example}]")
    elif facts.revision is None:
        problems.append(f"the database reports no migration revision ({SCHEMA_HINT})")
    if not problems and not any(facts.counts.values()):
        problems.append(f"the backup would have captured zero rows from {len(facts.counts)} tables "
                        f"({', '.join(sorted(facts.counts))}): there is nothing here to protect")
    return problems


def create_backup(database_url: str, dest_dir: str, *, kind: str = "auto", label: Optional[str] = None,
                  now: Optional[Callable[[], datetime]] = None) -> BackupInfo:
    """Dump the database into `dest_dir`, verify the dump, write its manifest. Raises BackupError on any problem."""
    if kind not in ("auto", "milestone"):
        raise ValueError("kind must be auto or milestone")
    dest = pathlib.Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    database = pgtools.parse(database_url).database or "database"
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (label or ""))[:40]
    name = f"{database}-{moment:%Y%m%dT%H%M%S%f}Z-{kind}" + (f"-{slug}" if slug else "")
    final, partial = dest / f"{name}.dump", dest / f"{name}.dump.partial"

    # Before anything is written: is this database worth dumping at all? Asked first so a server whose
    # migrations never ran leaves no file behind that later looks like a backup someone could restore.
    facts = _snapshot_facts(database_url)
    unusable = _problems(facts)
    if unusable:
        raise BackupError("refusing to write a backup that would protect nothing - " + "; ".join(unusable))

    done = pgtools.run("pg_dump", database_url, ["-Fc", "--no-owner", "--no-privileges", "-f", str(partial)])
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {done.stderr.strip()[:500]}")
    if "does not exist" in done.stderr:
        # pg_dump can exit 0 having skipped what it could not read (a table dropped under it mid-run).
        partial.unlink(missing_ok=True)
        raise BackupError(f"pg_dump could not read part of the database ({SCHEMA_HINT}): {done.stderr.strip()[:500]}")
    check = pgtools.run("pg_restore", database_url, ["--list"], include_db=False, positional=(partial,))
    if check.returncode != 0 or not check.stdout.strip():
        partial.unlink(missing_ok=True)
        raise BackupError(f"the dump could not be read back: {check.stderr.strip()[:500]}")
    written = partial.stat().st_size
    if written < MIN_PLAUSIBLE_DUMP_BYTES:
        partial.unlink(missing_ok=True)
        raise BackupError(f"the dump is suspiciously small ({written} bytes, expected at least "
                          f"{MIN_PLAUSIBLE_DUMP_BYTES}): it cannot hold this database")
    os.replace(partial, final)

    info = BackupInfo(name=name, path=str(final), manifest_path=str(dest / f"{name}.json"), kind=kind, label=label,
                      created_at=moment.isoformat(), size_bytes=final.stat().st_size, sha256=_sha256(final),
                      alembic_revision=facts.revision, server_version=facts.server_version, counts=facts.counts)
    manifest_partial = dest / f"{name}.json.partial"
    manifest_partial.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")
    os.replace(manifest_partial, info.manifest_path)
    logger.info("backup written: %s (%d bytes, %s)", name, info.size_bytes, facts.counts)
    return info


def list_backups(dest_dir: str) -> list[dict]:
    """Every backup that has a manifest, newest first."""
    out = []
    for manifest in pathlib.Path(dest_dir).glob("*.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("path") and pathlib.Path(data["path"]).exists():
                out.append(data)
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda d: d["created_at"], reverse=True)


def latest_manifest(dest_dir: str) -> Optional[dict]:
    found = list_backups(dest_dir)
    return found[0] if found else None


def verify_backup(dump_path: str) -> dict:
    """The dump still matches its manifest (size and SHA-256) and PostgreSQL can read it."""
    path = pathlib.Path(dump_path)
    manifest_file = path.with_suffix(".json")
    problems = []
    if not path.exists():
        return {"ok": False, "problems": ["the dump file is missing"]}
    manifest = None
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("size_bytes") != path.stat().st_size:
            problems.append("the file size differs from its manifest")
        if manifest.get("sha256") != _sha256(path):
            problems.append("the SHA-256 differs from its manifest: the file has been changed or damaged")
    else:
        problems.append("there is no manifest beside the dump")
    listing = pgtools.run("pg_restore", "postgresql://x@localhost/x", ["--list"], include_db=False, positional=(path,))
    if listing.returncode != 0:
        problems.append("PostgreSQL cannot read the dump")
    return {"ok": not problems, "problems": problems, "manifest": manifest}


def prune(dest_dir: str, keep: int) -> list[str]:
    """Delete the oldest AUTOMATIC backups beyond `keep`. Milestones are never touched. Returns what was removed."""
    autos = [b for b in list_backups(dest_dir) if b["kind"] == "auto"]
    removed = []
    for old in autos[max(keep, 1):]:
        for f in (old["path"], old["manifest_path"]):
            pathlib.Path(f).unlink(missing_ok=True)
        removed.append(old["name"])
    return removed


class BackupScheduler:
    """Runs `run_backup` every `interval` seconds of the injected clock. Tests pass a fake clock and call tick()
    to fast-forward hours in a moment; production calls run_forever(). A failed run does not stop the schedule:
    it is retried soon, and the next scheduled time is not pushed back."""

    def __init__(self, run_backup: Callable[[], BackupInfo], interval: float = DEFAULT_INTERVAL_SECONDS, *,
                 clock: Callable[[], float] = time.monotonic, after_success: Optional[Callable[[BackupInfo], None]] = None):
        self.run_backup, self.interval, self.clock, self.after_success = run_backup, float(interval), clock, after_success
        self.next_due = clock()  # the first backup is taken straight away
        self.failures = 0
        self.last_error: Optional[str] = None

    def tick(self) -> Optional[BackupInfo]:
        now = self.clock()
        if now < self.next_due:
            return None
        try:
            info = self.run_backup()
        except Exception as exc:  # noqa: BLE001 - a backup fault must never stop the schedule
            self.failures += 1
            self.last_error = str(exc)
            self.next_due = now + min(self.interval, RETRY_AFTER_FAILURE_SECONDS)
            logger.error("BACKUP FAILED (will retry), no backup file was created: %s", exc)
            return None
        self.last_error = None
        # Fixed rate: skip any intervals that were missed while the machine was busy or asleep.
        self.next_due += self.interval * max(1, int((now - self.next_due) // self.interval) + 1)
        if self.after_success:
            self.after_success(info)
        return info

    def run_forever(self, stop: threading.Event, poll_seconds: float = 1.0) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(poll_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.ha.backup", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("run", "once", "milestone", "list", "verify"))
    parser.add_argument("target", nargs="?", help="verify: the dump file")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--dir", default=os.getenv("BACKUP_DIR"), help="where backups go: a SECOND device (default $BACKUP_DIR)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("BACKUP_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
    parser.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP", DEFAULT_KEEP)))
    parser.add_argument("--label", help="milestone name, e.g. " + ", ".join(MILESTONES))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.action == "verify":
        if not args.target:
            parser.error("verify needs the dump file")
        result = verify_backup(args.target)
        print("OK" if result["ok"] else "PROBLEM: " + "; ".join(result["problems"]))
        return 0 if result["ok"] else 1
    if not args.dir:
        parser.error("no backup folder: set BACKUP_DIR or pass --dir")
    if args.action == "list":
        for b in list_backups(args.dir):
            print(f"{b['created_at']}  {b['kind']:9} {b['size_bytes']:>10} bytes  {b['name']}")
        return 0
    if not args.database_url:
        parser.error("no database: set DATABASE_URL or pass --database-url")
    if args.action in ("once", "milestone"):
        if args.action == "milestone" and not args.label:
            parser.error("a milestone needs --label")
        try:
            info = create_backup(args.database_url, args.dir, kind="auto" if args.action == "once" else "milestone", label=args.label)
        except BackupError as exc:
            # Loudly, and with a failing exit code: a backup command that "succeeds" without a usable
            # backup is the one failure nobody notices until the day they need to restore.
            logger.error("BACKUP FAILED, no backup file was created: %s", exc)
            return 1
        print(f"written: {info.path}")
        return 0

    # `run` is what docker-compose starts and leaves running. A database that cannot be backed up at all
    # is a setup fault, not a passing fault, so the job refuses to start rather than logging a retry every
    # thirty seconds for the rest of the day. Faults that appear LATER are still retried (see the scheduler).
    unusable = check_ready(args.database_url)
    if unusable:
        logger.error("BACKUP JOB REFUSING TO START, no backup will be taken: %s", "; ".join(unusable))
        return 1
    scheduler = BackupScheduler(lambda: create_backup(args.database_url, args.dir), args.interval,
                                after_success=lambda info: prune(args.dir, args.keep))
    logger.info("backup job started: every %s seconds into %s (keeping %s)", args.interval, args.dir, args.keep)
    try:
        scheduler.run_forever(threading.Event())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

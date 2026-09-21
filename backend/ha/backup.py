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
COUNTED_TABLES = ("activity_events", "audit_log", "students", "outbox", "exceptions", "sync_log", "conflict_events")


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


def _snapshot_facts(database_url: str) -> tuple[Optional[str], Optional[str], dict]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            revision = None
            try:
                revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                conn.rollback()
            counts = {}
            for table in COUNTED_TABLES:
                try:
                    counts[table] = int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
                except Exception:
                    conn.rollback()
            return revision, str(conn.execute(text("SHOW server_version")).scalar()), counts
    finally:
        engine.dispose()


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
    revision, server_version, counts = _snapshot_facts(database_url)

    done = pgtools.run("pg_dump", database_url, ["-Fc", "--no-owner", "--no-privileges", "-f", str(partial)])
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {done.stderr.strip()[:500]}")
    check = pgtools.run("pg_restore", database_url, ["--list"], include_db=False, positional=(partial,))
    if check.returncode != 0 or not check.stdout.strip():
        partial.unlink(missing_ok=True)
        raise BackupError(f"the dump could not be read back: {check.stderr.strip()[:500]}")
    os.replace(partial, final)

    info = BackupInfo(name=name, path=str(final), manifest_path=str(dest / f"{name}.json"), kind=kind, label=label,
                      created_at=moment.isoformat(), size_bytes=final.stat().st_size, sha256=_sha256(final),
                      alembic_revision=revision, server_version=server_version, counts=counts)
    manifest_partial = dest / f"{name}.json.partial"
    manifest_partial.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")
    os.replace(manifest_partial, info.manifest_path)
    logger.info("backup written: %s (%d bytes, %s)", name, info.size_bytes, counts)
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
            logger.error("backup failed (will retry): %s", exc)
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
        info = create_backup(args.database_url, args.dir, kind="auto" if args.action == "once" else "milestone", label=args.label)
        print(f"written: {info.path}")
        return 0
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

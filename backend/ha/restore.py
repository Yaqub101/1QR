"""Restore a backup onto a clean laptop (SYSTEM_SPEC 21: "restore a venue from a backup on a clean laptop").

    python -m backend.ha.restore <dump-file> --database-url postgresql://user:pw@localhost/convocation_db
    python -m backend.ha.restore --latest-from /mnt/backup --database-url ...          # the newest dump in a folder
    add --replace to overwrite a database that already has data (it is dropped first: only when you mean it)

Safety:
  * the dump is checked against its manifest (size and SHA-256) BEFORE anything is touched;
  * a database that already holds data is refused unless --replace is given;
  * after restoring, the row counts and the database revision are compared with the manifest, and the command
    reports (and exits non-zero) if they differ.
Nothing here needs the application to be running. Start the app afterwards and it will serve the restored data.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Optional

from sqlalchemy import create_engine, text

from backend.ha import backup, pgtools


class RestoreError(RuntimeError):
    pass


def _admin_engine(url: str):
    return create_engine(pgtools.maintenance_url(url), isolation_level="AUTOCOMMIT")


def _database_state(url: str) -> str:
    """'missing', 'empty' or 'has data'."""
    database = pgtools.parse(url).database
    admin = _admin_engine(url)
    try:
        with admin.connect() as conn:
            if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": database}).scalar():
                return "missing"
    finally:
        admin.dispose()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = conn.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")).scalar_one()
        return "has data" if tables else "empty"
    finally:
        engine.dispose()


def restore_backup(dump_path: str, target_url: str, *, replace: bool = False) -> dict:
    path = pathlib.Path(dump_path)
    checked = backup.verify_backup(str(path))
    if not checked["ok"]:
        raise RestoreError("this backup cannot be trusted: " + "; ".join(checked["problems"]))
    manifest = checked["manifest"]
    database = pgtools.parse(target_url).database
    state = _database_state(target_url)
    if state == "has data" and not replace:
        raise RestoreError(f"database {database!r} already holds data. Nothing was changed. "
                           f"Use --replace only if you really mean to overwrite it.")
    admin = _admin_engine(target_url)
    try:
        with admin.connect() as conn:
            if state in ("has data", "empty"):
                conn.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        admin.dispose()
    done = pgtools.run("pg_restore", target_url, ["--no-owner", "--no-privileges", "--exit-on-error"], positional=(path,))
    if done.returncode != 0:
        raise RestoreError(f"pg_restore failed: {done.stderr.strip()[:500]}")

    revision, _, counts = backup._snapshot_facts(target_url)
    mismatches = {t: (manifest["counts"].get(t), counts.get(t)) for t in manifest["counts"] if manifest["counts"].get(t) != counts.get(t)}
    if manifest.get("alembic_revision") != revision:
        mismatches["alembic_revision"] = (manifest.get("alembic_revision"), revision)
    return {"ok": not mismatches, "database": database, "restored_from": path.name, "counts": counts, "revision": revision,
            "mismatches": mismatches, "backup_taken_at": manifest["created_at"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.ha.restore", description=__doc__.split("\n\n")[0])
    parser.add_argument("dump", nargs="?", help="the .dump file")
    parser.add_argument("--latest-from", help="use the newest backup in this folder")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--replace", action="store_true", help="overwrite a database that already has data")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("no database: set DATABASE_URL or pass --database-url")
    dump: Optional[str] = args.dump
    if args.latest_from:
        latest = backup.latest_manifest(args.latest_from)
        if not latest:
            print("There are no backups in that folder.")
            return 2
        dump = latest["path"]
    if not dump:
        parser.error("give a dump file, or --latest-from a folder")
    try:
        result = restore_backup(dump, args.database_url, replace=args.replace)
    except (RestoreError, backup.BackupError, pgtools.ToolMissing) as exc:
        print(f"NOT RESTORED: {exc}")
        return 1
    print(json.dumps(result, indent=2, default=str))
    print("RESTORED AND VERIFIED" if result["ok"] else "RESTORED, BUT THE COUNTS DO NOT MATCH THE BACKUP - see above")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Rebuild central from the venues (SYSTEM_SPEC 18, 21): "if central is lost, all venues keep working and hold
their outboxes; central is then rebuilt from venue data."

    python -m backend.sync.rebuild --central-url postgresql://.../central \\
        --venue college=postgresql://.../college --venue stadium=postgresql://.../stadium \\
        --venue hall=postgresql://.../hall --master-from stadium --migrate

What it does, in order:
  1. (--migrate) brings an empty central database to the current schema (alembic upgrade head).
  2. Copies the student master, the ACTIVE QR tokens and the display snapshot from one venue when central has no
     students yet (every venue holds the full master). User accounts are NOT copied: recreate the central Admin with
     `python -m backend.seed`.
  3. Reads each venue's OWN events (its authoritative activities) and ingests them through the same idempotent code
     path as live sync (backend/sync/ingest.py). Running it twice, or on a central that is partly rebuilt, adds
     nothing twice. It reads events, not the outbox, so events that were never pushed are included.
  4. If central started EMPTY it gets a NEW EPOCH, so every venue's pull worker restarts its cursor and re-pulls
     (the events it already holds are DUPLICATEs). Without this a venue's old cursor would sit beyond the new
     numbering and it would silently miss events.
  5. Runs reconciliation, then VERIFIES: per venue, events read = events now at central. Exit status 1 if not.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from typing import Optional

from sqlalchemy import create_engine, text

from backend.security.ownership import VENUES
from backend.sync import reconcile as reconcile_svc
from backend.sync.ingest import ingest_events

logger = logging.getLogger("backend.sync")
PAGE = 500
MASTER_TABLES = (  # foreign-key order
    ("students", "SELECT * FROM students ORDER BY prn"),
    ("qr_tokens", "SELECT * FROM qr_tokens WHERE active ORDER BY id"),
    ("display_snapshot", "SELECT * FROM display_snapshot"),
)


def _copy_master(source, central) -> dict:
    copied = {}
    with source.connect() as src, central.begin() as dst:
        for table, query in MASTER_TABLES:
            rows = [dict(r) for r in src.execute(text(query)).mappings()]
            if not rows:
                copied[table] = 0
                continue
            columns = [c for c in rows[0] if not (table == "qr_tokens" and c == "id")]  # tokens get fresh ids
            sql = text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)}) ON CONFLICT DO NOTHING")
            dst.execute(sql, [{c: r[c] for c in columns} for r in rows])
            copied[table] = len(rows)
    return copied


def rebuild_central(central, venues: dict, *, master_from: Optional[str] = None) -> dict:
    """`central` and each value of `venues` are SQLAlchemy engines. Returns a report; report["ok"] says whether it verified."""
    with central.connect() as conn:
        had_events = int(conn.execute(text("SELECT count(*) FROM activity_events")).scalar_one())
        had_students = int(conn.execute(text("SELECT count(*) FROM students")).scalar_one())
    report: dict = {"master_copied": {}, "venues": {}, "started_empty": had_events == 0}
    if had_students == 0 and master_from:
        report["master_copied"] = _copy_master(venues[master_from], central)

    for venue, source in venues.items():
        read = accepted = duplicates = other = 0
        last_seq = 0
        while True:
            with source.connect() as src:
                page = [r[0] for r in src.execute(
                    text("SELECT to_jsonb(e) FROM activity_events e WHERE venue_id = :v AND venue_seq > :s ORDER BY venue_seq LIMIT :n"),
                    {"v": venue, "s": last_seq, "n": PAGE})]
            if not page:
                break
            with central.begin() as dst:
                results = ingest_events(dst, page, expected_venue=venue, log_arrivals=True, source="rebuild")
            read += len(page)
            last_seq = page[-1]["venue_seq"]
            for r in results:
                accepted += r.status == "ACCEPTED"
                duplicates += r.status == "DUPLICATE"
                other += r.status not in ("ACCEPTED", "DUPLICATE")
        with central.connect() as dst:
            held = int(dst.execute(text("SELECT count(*) FROM activity_events WHERE venue_id = :v"), {"v": venue}).scalar_one())
        report["venues"][venue] = {"read_from_venue": read, "accepted": accepted, "already_there": duplicates,
                                   "refused_or_held": other, "now_at_central": held, "ok": held == read and other == 0}

    if report["started_empty"]:
        with central.begin() as dst:
            report["new_epoch"] = dst.execute(text("UPDATE sync_meta SET value = gen_random_uuid()::text, updated_at = now() "
                                                   "WHERE key = 'epoch' RETURNING value")).scalar_one()
    report["reconciliation"] = reconcile_svc.reconcile(central)
    report["ok"] = all(v["ok"] for v in report["venues"].values())
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.sync.rebuild", description="Rebuild central from the venue databases.")
    parser.add_argument("--central-url", default=os.getenv("DATABASE_URL"), help="the (new, empty) CENTRAL database")
    parser.add_argument("--venue", action="append", default=[], metavar="NAME=URL", help="a venue database; repeat per venue")
    parser.add_argument("--master-from", choices=VENUES, help="copy students / active tokens / display snapshot from this venue")
    parser.add_argument("--migrate", action="store_true", help="run `alembic upgrade head` on central first")
    args = parser.parse_args(argv)
    if not args.central_url or not args.venue:
        parser.error("give --central-url and at least one --venue NAME=URL")
    urls = {}
    for item in args.venue:
        name, _, url = item.partition("=")
        if name not in VENUES or not url:
            parser.error(f"bad --venue {item!r}; use college=URL, stadium=URL or hall=URL")
        urls[name] = url
    if args.master_from and args.master_from not in urls:
        parser.error("--master-from must be one of the --venue names")
    if args.migrate:
        env = {**os.environ, "DATABASE_URL": args.central_url, "MODE": "central"}
        done = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env,
                              cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        if done.returncode != 0:
            print("The database could not be brought up to date; stopping.")
            return 2
    report = rebuild_central(create_engine(args.central_url), {n: create_engine(u) for n, u in urls.items()}, master_from=args.master_from)
    for venue, r in report["venues"].items():
        print(f"{venue:8} read {r['read_from_venue']:>6}  now at central {r['now_at_central']:>6}  {'OK' if r['ok'] else 'MISMATCH'}")
    print(f"master copied: {report['master_copied'] or 'no'}   reconciliation: {report['reconciliation']}")
    print("REBUILD VERIFIED" if report["ok"] else "REBUILD DID NOT VERIFY - see the lines above")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

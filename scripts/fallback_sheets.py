"""Print the paper fallback sheets: the student list with sequence number and seat, one sheet per activity.

SYSTEM_SPEC 18 ("Last resort"): each station keeps a printed sheet so the event can carry on on paper, with the
entries typed in later. This reads the CURRENT master list from the database and writes one printable page per
activity (open it in a browser and print, landscape).

    python scripts/fallback_sheets.py                              # all seven sheets into exports/fallback-sheets/
    python scripts/fallback_sheets.py --venue stadium              # only the Stadium's four sheets
    python scripts/fallback_sheets.py --database-url postgresql://user:password@host:5432/db --out D:/sheets

Rules it keeps:
  * READ ONLY. It never writes to the database.
  * Every student in the master list is on every sheet, in sequence order, so the row count equals the master
    count. An inactive student is printed and marked DO NOT SERVE rather than dropped, so a missing name never
    tempts anyone to add someone by hand. It refuses to write anything if the counts disagree.
  * No QR token and no photo is printed (AGENTS.md golden rule 1). The sheets are still student data: keep them
    at the station and hand them to the Admin afterwards. `exports/` is git-ignored.
"""
from __future__ import annotations

import argparse
import html
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from backend.security.ownership import ACTIVITIES, ACTIVITY_LABEL, ACTIVITY_OWNER, VENUE_LABEL  # noqa: E402

DEFAULT_OUT = "exports/fallback-sheets"

# The stage follows the ORDER OF QUEUE CONFIRMATION, never the sequence number (SYSTEM_SPEC 11 C4), so paper needs it.
EXTRA_COLUMN = {"QUEUE": "Queue no.<br>(write, in order of arrival)"}
NOTE = {
    "QUEUE": "Number students 1, 2, 3 in the order they join the queue. The stage follows this order, "
             "not the sequence number.",
    "STAGE": "Call students in the order written on the Queue sheet, not in sequence-number order.",
}

CSS = """
@page { size: A4 landscape; margin: 10mm; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; color: #000; margin: 0; }
h1 { font-size: 18pt; margin: 0 0 2mm; }
.meta, .howto { margin: 0 0 2mm; }
.confidential { font-weight: bold; }
table { border-collapse: collapse; width: 100%; }
thead { display: table-header-group; }
th, td { border: 1px solid #000; padding: 2mm 1.5mm; text-align: left; vertical-align: middle; }
th { background: #ddd; }
tr { break-inside: avoid; }
td.num { text-align: right; white-space: nowrap; }
td.tick { width: 14mm; text-align: center; font-size: 16pt; }
td.time { width: 24mm; }
td.initials { width: 20mm; }
td.extra { width: 34mm; }
tr.inactive td { background: #eee; text-decoration: line-through; }
tr.inactive td.warn { text-decoration: none; font-weight: bold; }
""".strip()


def _cell(value) -> str:
    return html.escape("" if value is None else str(value))


def render_sheet(activity: str, students: list[dict], *, event_name: str, generated: str) -> str:
    venue = VENUE_LABEL[ACTIVITY_OWNER[activity]]
    label = ACTIVITY_LABEL[activity]
    extra = EXTRA_COLUMN.get(activity)
    body = []
    for s in students:
        inactive = s["status"] != "ACTIVE"
        cells = [
            f'<td class="num">{_cell(s["sequence_no"])}</td>',
            f'<td>{_cell(s["seat_no"])}</td>',
            f'<td>{_cell(s["prn"])}</td>',
            f'<td>{_cell(s["name"])}</td>',
            f'<td>{_cell(s["programme"])}</td>',
            f'<td>{_cell(s["school"])}</td>',
        ]
        if extra:
            cells.append('<td class="extra"></td>')
        if inactive:
            cells.append('<td class="warn" colspan="3">INACTIVE &mdash; DO NOT SERVE, CALL ADMIN</td>')
        else:
            cells.append('<td class="tick">&#9744;</td><td class="time"></td><td class="initials"></td>')
        css = "student inactive" if inactive else "student"
        body.append(f'<tr class="{css}" data-prn="{html.escape(s["prn"], quote=True)}">{"".join(cells)}</tr>')
    head = ["Seq", "Seat", "PRN", "Name", "Programme", "School"] + ([extra] if extra else []) + ["Done", "Time", "Initials"]
    note = f'<p class="howto"><strong>{html.escape(NOTE[activity])}</strong></p>' if activity in NOTE else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(label)} &mdash; {venue} &mdash; paper sheet</title>
<style>
{CSS}
</style>
</head>
<body>
<h1>{html.escape(label)} &mdash; {venue}</h1>
<p class="meta">{html.escape(event_name)} &middot; {len(students)} students &middot; printed {html.escape(generated)}</p>
<p class="howto">Use this only if the screen or server is not working. Tick <strong>Done</strong>, write the time and your
initials. Do not add students that are not on this list. Give the sheet to the Admin at the end: it is typed in later.</p>
{note}
<p class="confidential">CONFIDENTIAL &mdash; student data. Keep it at the station. Return it to the Admin after the event.</p>
<table>
<thead><tr>{"".join(f"<th>{h}</th>" for h in head)}</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody>
</table>
</body>
</html>
"""


def read_master(database_url: str) -> tuple[list[dict], int, str]:
    """(students in sequence order, count taken by a separate query, event name) from ONE consistent snapshot."""
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))  # a snapshot; never writes
            rows = conn.execute(text(
                "SELECT prn, name, programme, school, sequence_no, seat_no, status FROM students ORDER BY sequence_no"
            )).mappings().all()
            count = conn.execute(text("SELECT count(*) FROM students")).scalar_one()
            event = conn.execute(text("SELECT event_name FROM settings WHERE id = 1")).scalar()
        return [dict(r) for r in rows], int(count), event or "Convocation"
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Print the paper fallback sheets (one per activity) from the current database.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="default: $DATABASE_URL")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"folder for the sheets (default {DEFAULT_OUT})")
    parser.add_argument("--venue", choices=sorted(VENUE_LABEL), help="only the sheets for this venue's activities")
    parser.add_argument("--event-name", default=os.getenv("EVENT_NAME"), help="title on each sheet (default: the event name in the database)")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("no database: set DATABASE_URL or pass --database-url")

    try:
        students, master_count, db_event_name = read_master(args.database_url)
    except Exception as exc:  # a plain sentence for the person at the keyboard; the detail follows
        print(f"Could not read the student list from the database. Is the server running?\n  ({exc.__class__.__name__}: {exc})")
        return 1
    if master_count == 0:
        print("The student list is empty, so there is nothing to print. Import the master list first.")
        return 1
    if len(students) != master_count:
        print(f"The list changed while it was being read ({len(students)} rows, {master_count} students). Nothing was written; run it again.")
        return 1

    offset = timezone(timedelta(minutes=int(os.getenv("EVENT_UTC_OFFSET_MINUTES", "330"))))
    generated = datetime.now(offset).strftime("%d %b %Y, %I:%M %p (UTC%z)")
    event_name = args.event_name or db_event_name
    out = pathlib.Path(args.out)

    pages = {}
    for number, activity in enumerate(ACTIVITIES, start=1):
        if args.venue and ACTIVITY_OWNER[activity] != args.venue:
            continue
        page = render_sheet(activity, students, event_name=event_name, generated=generated)
        printed = page.count('data-prn="')
        if printed != master_count:  # the one thing a paper sheet must never get wrong: a missing student
            print(f"STOP: the {ACTIVITY_LABEL[activity]} sheet has {printed} rows but the master list has {master_count}. Nothing was written.")
            return 1
        pages[f"{number}_{activity}.html"] = page

    out.mkdir(parents=True, exist_ok=True)
    for name, page in pages.items():
        (out / name).write_text(page, encoding="utf-8")
        print(f"written: {out / name}  ({master_count} students)")
    inactive = sum(1 for s in students if s["status"] != "ACTIVE")
    print(f"OK: {len(pages)} sheet(s), each with {master_count} rows = the master count"
          + (f" ({inactive} inactive, marked DO NOT SERVE)." if inactive else "."))
    print("These sheets hold student data. Print them, then keep the files off shared drives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

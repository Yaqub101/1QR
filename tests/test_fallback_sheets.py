"""Phase 18 + 20a: the paper fallback sheets (SYSTEM_SPEC 18 "Last resort", TODO Phase 18 "Print: the student list
with sequence/seat per station").

ONE basic test: run the generator the way the IT lead would (`python scripts/fallback_sheets.py`) against a
database whose contents are known, and check that every printed sheet has exactly one row per student in the
master list, in sequence order. The expected answers come from raw SQL and hand-written lists, not from the
generator. Two things that must never go wrong on paper are checked in the same run: a QR token is never printed
(golden rule 1), and a student's name can never break the page.
"""
import html
import re
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import TEST_DB_URL
from tests.test_schema import REPO_ROOT, engine  # noqa: F401  (engine is a pytest fixture: a fresh migrated schema)

# One sheet per activity, in journey order (SYSTEM_SPEC section 2). Written out by hand.
SHEETS = [
    "1_REGISTRATION.html", "2_THOBE_ALLOCATION.html", "3_SEATING.html", "4_QUEUE.html",
    "5_STAGE.html", "6_THOBE_RETURN.html", "7_LUNCH.html",
]
AWKWARD_NAME = 'Anil <i>&</i> "Kumar" O\'Brien'


def test_every_sheet_has_one_row_per_student_in_the_master_list(engine, tmp_path):
    # Sequence numbers are deliberately NOT in insertion order, one student is inactive and has no seat.
    students = [  # (prn, name, sequence_no, seat_no, status)
        ("P05", "Fifth by insertion", 3, "A-03", "ACTIVE"),
        ("P01", AWKWARD_NAME, 12, "B-12", "ACTIVE"),
        ("P02", "अनिल कुमार", 1, "A-01", "ACTIVE"),
        ("P03", "Left the course", 7, None, "INACTIVE"),
        ("P04", "José Müller", 2, "A-02", "ACTIVE"),
    ]
    tokens = {}
    with engine.begin() as c:
        for prn, name, seq, seat, status in students:
            sid = c.execute(
                text("INSERT INTO students (prn, name, programme, school, sequence_no, seat_no, status) "
                     "VALUES (:p, :n, 'B.Tech', 'School of Engineering', :q, :s, :st) RETURNING id"),
                {"p": prn, "n": name, "q": seq, "s": seat, "st": status},
            ).scalar_one()
            tokens[prn] = f"tok_{prn}_" + "x9Qz" * 8  # long and unmistakable, so any leak is found by a plain search
            c.execute(text("INSERT INTO qr_tokens (student_id, token) VALUES (:s, :t)"), {"s": sid, "t": tokens[prn]})

    with engine.connect() as c:
        master_count = c.execute(text("SELECT count(*) FROM students")).scalar_one()
        prns_in_sequence = c.execute(text("SELECT prn FROM students ORDER BY sequence_no")).scalars().all()
    assert master_count == len(students) == 5

    run = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fallback_sheets.py"),
         "--database-url", TEST_DB_URL, "--out", str(tmp_path), "--event-name", "Test Convocation"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert run.returncode == 0, f"generator failed:\n{run.stdout}\n{run.stderr}"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(SHEETS)

    for name in SHEETS:
        page = (tmp_path / name).read_text(encoding="utf-8")
        listed = [html.unescape(p) for p in re.findall(r'<tr class="student[^"]*" data-prn="([^"]*)"', page)]
        assert len(listed) == master_count, f"{name}: {len(listed)} rows, master list has {master_count}"
        assert listed == prns_in_sequence, f"{name}: rows are not in sequence order"
        assert f"{master_count} students" in page, f"{name}: the printed total is missing"
        assert "INACTIVE" in page, f"{name}: the inactive student must be marked, not silently dropped"
        assert not any(t in page for t in tokens.values()), f"{name}: a QR token was printed"
        assert "<i>&</i>" not in page and html.escape(AWKWARD_NAME) in page, f"{name}: a name was not escaped"
        assert "B-12" in page and "अनिल कुमार" in page

"""backend/snapshot.py — Phase 3 display_snapshot management.

SYSTEM_SPEC.md golden rule 10:
  "The LED reads only the display snapshot. Never expose PRN, phone, email
  or internal fields."

After freeze_display_data() runs, subsequent changes to the students table do
NOT alter display_snapshot. Only another explicit call to freeze_display_data()
updates it. This is enforced by design: there are no triggers or FK cascades
from students to display_snapshot — the snapshot is populated only here.

Migration 0010 adds the other half of the rule: once a student has been frozen,
neither that student's master row nor the snapshot may be changed by anything
that has not announced itself with begin_master_patch_txn(). The two sanctioned
doors are this freeze and backend/master_patch.py; everything else is refused by
the database, not merely by convention.
"""
from __future__ import annotations

import dataclasses
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclasses.dataclass
class FreezeSummary:
    frozen_count: int


def begin_master_patch_txn(conn: Connection) -> None:
    """Mark THIS transaction as a sanctioned master-data change, so migration 0010's guard allows it.

    `SET LOCAL`, so it lasts exactly as long as the transaction and cannot leak to the next caller
    that borrows the same pooled connection."""
    conn.execute(text("SELECT set_config('app.master_patch', 'on', true)"))


def freeze_display_data(
    conn: Connection,
    operator_id: str | None = None,
) -> FreezeSummary:
    """Populate / refresh display_snapshot from students.

    For each student, upserts a row in display_snapshot with:
      display_name, programme, school, award, photo_path, frozen_at = now().

    Subsequent edits to students do NOT propagate to display_snapshot.
    Only another explicit call to this function does.

    Logs action 'FREEZE_DISPLAY_SNAPSHOT' to audit_log.
    """
    try:
        begin_master_patch_txn(conn)  # the freeze is one of the two doors migration 0010 allows
        # Upsert from students → display_snapshot
        conn.execute(
            text(
                """
                INSERT INTO display_snapshot
                    (student_id, display_name, programme, school, award, photo_path, frozen_at)
                SELECT
                    id,
                    name,
                    programme,
                    school,
                    awards,
                    photo_path,
                    now()
                FROM students
                ON CONFLICT (student_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    programme    = EXCLUDED.programme,
                    school       = EXCLUDED.school,
                    award        = EXCLUDED.award,
                    photo_path   = EXCLUDED.photo_path,
                    frozen_at    = EXCLUDED.frozen_at
                """
            )
        )

        frozen_count = conn.execute(
            text("SELECT count(*) FROM display_snapshot")
        ).scalar()

        details = {"frozen_count": frozen_count}
        if operator_id:
            details["operator_id"] = str(operator_id)

        conn.execute(
            text(
                "INSERT INTO audit_log (action, details) VALUES ('FREEZE_DISPLAY_SNAPSHOT', CAST(:d AS jsonb))"
            ),
            {"d": json.dumps(details)},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return FreezeSummary(frozen_count=frozen_count)

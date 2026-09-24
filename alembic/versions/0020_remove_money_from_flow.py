"""0020_remove_money_from_flow: replace student_status view for 7-activity flow

Money activities (MONEY_RECEIVED, MONEY_RETURNED) remain valid in the
activity_events schema for historical data, but the running ceremony flow
no longer uses them.  This migration:

1. Replaces the student_status view with a 7-activity version whose labels
   no longer mention money.
2. Tightens the waiver constraint back to THOBE_RETURN only (money waivers
   are not issued in the 7-activity flow).

Revision ID: 0020_remove_money_from_flow
Revises: 0019_queue_caller_stage
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020_remove_money_from_flow"
down_revision: Union[str, Sequence[str], None] = "0019_queue_caller_stage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 7-activity view (no money columns).
# step:
#   0  not reported
#   1  reported, robe pending
#   2  robe received, not queued (or seated)
#   3  seated / not queued   (optional step: skipped when no SEATING event)
#   4  queued, degree pending
#   5  degree received, robe not returned
#   6  robe returned, lunch eligible
#   7  exited (lunch claimed)
_VIEW = """
    CREATE OR REPLACE VIEW student_status AS
    WITH active AS (
        SELECT e.student_id, e.activity
        FROM activity_events e
        WHERE e.kind IN ('COMPLETE','WAIVER')
          AND NOT EXISTS (
              SELECT 1 FROM activity_events r
              WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id
                AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
    ),
    done AS (
        SELECT s.id, s.prn,
               coalesce(bool_or(a.activity = 'REGISTRATION'), false)     AS reported,
               coalesce(bool_or(a.activity = 'THOBE_ALLOCATION'), false) AS robe,
               coalesce(bool_or(a.activity = 'SEATING'), false)          AS seated,
               coalesce(bool_or(a.activity = 'QUEUE'), false)            AS queued,
               coalesce(bool_or(a.activity = 'STAGE'), false)            AS staged,
               coalesce(bool_or(a.activity = 'THOBE_RETURN'), false)     AS robe_back,
               coalesce(bool_or(a.activity = 'LUNCH'), false)            AS lunch
        FROM students s LEFT JOIN active a ON a.student_id = s.id
        GROUP BY s.id, s.prn
    )
    SELECT id AS student_id,
           prn,
           (CASE
               WHEN lunch                    THEN 7
               WHEN staged AND robe_back     THEN 6
               WHEN staged                   THEN 5
               WHEN queued                   THEN 4
               WHEN robe AND seated          THEN 3
               WHEN robe                     THEN 2
               WHEN reported                 THEN 1
               ELSE 0
           END)::smallint AS step,
           CASE
               WHEN lunch                    THEN 'EXITED'
               WHEN staged AND robe_back     THEN 'LUNCH ELIGIBLE'
               WHEN staged                   THEN 'ROBE NOT RETURNED'
               WHEN queued                   THEN 'DEGREE NOT RECEIVED'
               WHEN robe AND seated          THEN 'SEATED / NOT QUEUED'
               WHEN robe                     THEN 'ROBE RECEIVED / NOT QUEUED'
               WHEN reported                 THEN 'REPORTED / ROBE PENDING'
               ELSE 'REGISTERED / NOT REPORTED'
           END AS status
    FROM done
"""

# Restore the 0017 money-aware view on downgrade.
_VIEW_BEFORE = """
    CREATE OR REPLACE VIEW student_status AS
    WITH active AS (
        SELECT e.student_id, e.activity
        FROM activity_events e
        WHERE e.kind IN ('COMPLETE','WAIVER')
          AND NOT EXISTS (
              SELECT 1 FROM activity_events r
              WHERE r.kind = 'REVERSAL' AND r.student_id = e.student_id
                AND r.activity = e.activity AND r.completion_cycle = e.completion_cycle)
    ),
    done AS (
        SELECT s.id, s.prn,
               coalesce(bool_or(a.activity = 'REGISTRATION'), false)     AS reported,
               coalesce(bool_or(a.activity = 'THOBE_ALLOCATION'), false) AS robe,
               coalesce(bool_or(a.activity = 'MONEY_RECEIVED'), false)   AS money,
               coalesce(bool_or(a.activity = 'SEATING'), false)          AS seated,
               coalesce(bool_or(a.activity = 'QUEUE'), false)            AS queued,
               coalesce(bool_or(a.activity = 'STAGE'), false)            AS staged,
               coalesce(bool_or(a.activity = 'THOBE_RETURN'), false)     AS robe_back,
               coalesce(bool_or(a.activity = 'MONEY_RETURNED'), false)   AS money_back,
               coalesce(bool_or(a.activity = 'LUNCH'), false)            AS lunch
        FROM students s LEFT JOIN active a ON a.student_id = s.id
        GROUP BY s.id, s.prn
    )
    SELECT id AS student_id,
           prn,
           (CASE
               WHEN lunch                              THEN 7
               WHEN staged AND robe_back AND money_back THEN 6
               WHEN staged                             THEN 5
               WHEN queued                             THEN 4
               WHEN robe AND money AND seated          THEN 3
               WHEN robe AND money                     THEN 2
               WHEN reported                           THEN 1
               ELSE 0
           END)::smallint AS step,
           CASE
               WHEN lunch                               THEN 'EXITED'
               WHEN staged AND robe_back AND money_back THEN 'LUNCH ELIGIBLE'
               WHEN staged AND robe_back                THEN 'MONEY NOT RETURNED'
               WHEN staged AND money_back               THEN 'ROBE NOT RETURNED'
               WHEN staged                              THEN 'ROBE AND MONEY NOT RETURNED'
               WHEN queued                              THEN 'DEGREE NOT RECEIVED'
               WHEN robe AND money AND seated           THEN 'SEATED / NOT QUEUED'
               WHEN robe AND money                      THEN 'ROBE AND MONEY RECEIVED / NOT QUEUED'
               WHEN reported AND robe                   THEN 'REPORTED / MONEY PENDING'
               WHEN reported AND money                  THEN 'REPORTED / ROBE PENDING'
               WHEN reported                            THEN 'REPORTED / ROBE AND MONEY PENDING'
               ELSE 'REGISTERED / NOT REPORTED'
           END AS status
    FROM done
"""


def upgrade() -> None:
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
    # Tighten waiver: only THOBE_RETURN waivers are issued in the 7-activity flow
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT activity_events_waiver_is_return_only")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_waiver_is_return_only "
               "CHECK (kind <> 'WAIVER' OR activity = 'THOBE_RETURN')")


def downgrade() -> None:
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW_BEFORE.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT activity_events_waiver_is_return_only")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_waiver_is_return_only "
               "CHECK (kind <> 'WAIVER' OR activity IN ('THOBE_RETURN', 'MONEY_RETURNED'))")

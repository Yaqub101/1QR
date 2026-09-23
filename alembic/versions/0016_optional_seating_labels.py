"""student_status labels: Seating is optional, so no label says "NOT SEATED"

Since the role/flow redesign Seating is an optional checkpoint that never blocks anything, and the Queue
needs only the robe. The two labels that read as a pending seat now say what is done and that the Queue is
next:

    step 2  "NOT SEATED"  ->  "ROBE RECEIVED / NOT QUEUED"
    step 3  "NOT QUEUED"  ->  "SEATED / NOT QUEUED"

CREATE OR REPLACE VIEW, same columns and types; no data is touched.

Revision ID: 0016_optional_seating_labels
Revises: 0015_caller_role
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_optional_seating_labels"
down_revision: Union[str, Sequence[str], None] = "0015_caller_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = """
        CREATE OR REPLACE VIEW student_status AS
        SELECT s.id AS student_id,
               s.prn,
               COALESCE(max(done.step), 0)::smallint AS step,
               CASE COALESCE(max(done.step), 0)
                   WHEN 0 THEN 'REGISTERED / NOT REPORTED'
                   WHEN 1 THEN 'REPORTED / ROBE NOT RECEIVED'
                   WHEN 2 THEN '{step2}'
                   WHEN 3 THEN '{step3}'
                   WHEN 4 THEN 'DEGREE NOT RECEIVED'
                   WHEN 5 THEN 'ROBE NOT RETURNED'
                   WHEN 6 THEN 'LUNCH ELIGIBLE'
                   WHEN 7 THEN 'EXITED'
               END AS status
        FROM students s
        LEFT JOIN (
            SELECT e.student_id, activity_step(e.activity) AS step
            FROM activity_events e
            WHERE e.kind IN ('COMPLETE','WAIVER')
              AND NOT EXISTS (
                  SELECT 1 FROM activity_events r
                  WHERE r.kind = 'REVERSAL'
                    AND r.student_id = e.student_id
                    AND r.activity = e.activity
                    AND r.completion_cycle = e.completion_cycle)
        ) done ON done.student_id = s.id
        GROUP BY s.id, s.prn
        """


def upgrade() -> None:
    op.execute(_VIEW.format(step2="ROBE RECEIVED / NOT QUEUED", step3="SEATED / NOT QUEUED"))


def downgrade() -> None:
    op.execute(_VIEW.format(step2="NOT SEATED", step3="NOT QUEUED"))

"""student_status labels: "Thobe" is now called "Robe"

Only the two human-readable status labels change (CREATE OR REPLACE VIEW, same
columns and types). The activity codes THOBE_ALLOCATION / THOBE_RETURN stay as
they are: they are stored in the append-only event history, roles and
constraints, and are never shown to a person.

Revision ID: 0013_robe_status_labels
Revises: 0012_single_server_pivot
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_robe_status_labels"
down_revision: Union[str, Sequence[str], None] = "0012_single_server_pivot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = """
        CREATE OR REPLACE VIEW student_status AS
        SELECT s.id AS student_id,
               s.prn,
               COALESCE(max(done.step), 0)::smallint AS step,
               CASE COALESCE(max(done.step), 0)
                   WHEN 0 THEN 'REGISTERED / NOT REPORTED'
                   WHEN 1 THEN 'REPORTED / {word} NOT RECEIVED'
                   WHEN 2 THEN 'NOT SEATED'
                   WHEN 3 THEN 'NOT QUEUED'
                   WHEN 4 THEN 'DEGREE NOT RECEIVED'
                   WHEN 5 THEN '{word} NOT RETURNED'
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
    op.execute(_VIEW.format(word="ROBE"))


def downgrade() -> None:
    op.execute(_VIEW.format(word="THOBE"))

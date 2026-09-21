"""student_status: status derived from events, never stored (Phase 2)

SYSTEM_SPEC section 5. A student's status is a pure function of the SET of
active completions (COMPLETE or WAIVER events that have not been reversed):
the furthest journey step reached decides the label, so the same events give
the same status in whatever order sync delivers them.

Revision ID: 0004_student_status_view
Revises: 0003_integrity_triggers
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_student_status_view"
down_revision: Union[str, Sequence[str], None] = "0003_integrity_triggers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION activity_step(a text) RETURNS smallint
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
            SELECT CASE a
                WHEN 'REGISTRATION'     THEN 1
                WHEN 'THOBE_ALLOCATION' THEN 2
                WHEN 'SEATING'          THEN 3
                WHEN 'QUEUE'            THEN 4
                WHEN 'STAGE'            THEN 5
                WHEN 'THOBE_RETURN'     THEN 6
                WHEN 'LUNCH'            THEN 7
            END::smallint
        $$
        """
    )
    op.execute(
        """
        CREATE VIEW student_status AS
        SELECT s.id AS student_id,
               s.prn,
               COALESCE(max(done.step), 0)::smallint AS step,
               CASE COALESCE(max(done.step), 0)
                   WHEN 0 THEN 'REGISTERED / NOT REPORTED'
                   WHEN 1 THEN 'REPORTED / THOBE NOT RECEIVED'
                   WHEN 2 THEN 'NOT SEATED'
                   WHEN 3 THEN 'NOT QUEUED'
                   WHEN 4 THEN 'DEGREE NOT RECEIVED'
                   WHEN 5 THEN 'THOBE NOT RETURNED'
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
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS student_status")
    op.execute("DROP FUNCTION IF EXISTS activity_step(text)")

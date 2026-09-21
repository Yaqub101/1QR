"""exceptions: only ever resolved, never edited or deleted (Phase 13)

The Admin's "resolve" action is the one legitimate change to an exceptions row: OPEN -> RESOLVED, filling in
who / when / the note. Everything else about a row is the record of what was found and stays as written.
This trigger makes that true for every role and every statement, not only for the API:

  * DELETE and TRUNCATE are refused;
  * a RESOLVED row is final (it cannot be re-resolved or reopened, so a resolution note is never overwritten);
  * type, student, venue, event, details and created_at can never change.

No column or table is added for the Admin corrections themselves: a correction is a row in the existing
append-only activity_events table (kind REVERSAL / WAIVER, see 0002/0003) and an audit_log row. The trigger
is compatible with the Phase 2 rules (the resolution CHECK still runs after it).

Revision ID: 0007_exceptions_guard
Revises: 0006_stage_state
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_exceptions_guard"
down_revision: Union[str, Sequence[str], None] = "0006_stage_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION exceptions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status = 'RESOLVED' THEN
                    RAISE EXCEPTION 'a resolved exception is final' USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.type IS DISTINCT FROM OLD.type
                   OR NEW.student_id IS DISTINCT FROM OLD.student_id
                   OR NEW.venue_id IS DISTINCT FROM OLD.venue_id
                   OR NEW.event_id IS DISTINCT FROM OLD.event_id
                   OR NEW.details IS DISTINCT FROM OLD.details
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'an exception can only be resolved, never edited' USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'exceptions are never deleted: % is not permitted', TG_OP USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER exceptions_guard_row BEFORE UPDATE OR DELETE ON exceptions "
        "FOR EACH ROW EXECUTE FUNCTION exceptions_guard()"
    )
    op.execute(
        "CREATE TRIGGER exceptions_guard_truncate BEFORE TRUNCATE ON exceptions "
        "FOR EACH STATEMENT EXECUTE FUNCTION exceptions_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS exceptions_guard_row ON exceptions")
    op.execute("DROP TRIGGER IF EXISTS exceptions_guard_truncate ON exceptions")
    op.execute("DROP FUNCTION IF EXISTS exceptions_guard()")

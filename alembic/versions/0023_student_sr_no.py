"""0023_student_sr_no: add sr_no to students and extend master_data_guard

Revision ID: 0023_student_sr_no
Revises: 0022_stage_stale_lock_guard
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023_student_sr_no"
down_revision: Union[str, Sequence[str], None] = "0022_stage_stale_lock_guard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_MASTER_COLUMNS = [
    "prn", "name", "programme", "school", "photo_path", "awards",
    "sequence_no", "seat_no", "status", "email", "mobile"
]
_NEW_MASTER_COLUMNS = _OLD_MASTER_COLUMNS + ["sr_no"]


def _guard_function_sql(columns: list[str]) -> str:
    new = ", ".join(f"NEW.{c}" for c in columns)
    old = ", ".join(f"OLD.{c}" for c in columns)
    return f"""
        CREATE OR REPLACE FUNCTION master_data_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                RAISE EXCEPTION 'display data is frozen: it cannot be emptied'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF current_setting('app.master_patch', true) = 'on' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;

            IF TG_TABLE_NAME = 'display_snapshot' THEN
                RAISE EXCEPTION
                    'display data is frozen: change it with a logged master patch, or run the freeze again'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF EXISTS (SELECT 1 FROM display_snapshot d WHERE d.student_id = OLD.id)
               AND ({new}) IS DISTINCT FROM ({old}) THEN
                RAISE EXCEPTION
                    'this student''s display data is frozen: change the master record with a logged master patch'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """


def upgrade() -> None:
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS sr_no text")
    op.execute(_guard_function_sql(_NEW_MASTER_COLUMNS))


def downgrade() -> None:
    op.execute(_guard_function_sql(_OLD_MASTER_COLUMNS))
    op.execute("ALTER TABLE students DROP COLUMN IF EXISTS sr_no")

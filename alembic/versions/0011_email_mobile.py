"""email and mobile on students, and the frozen-data guard extended to cover them

The saved import preset for the university's "Student Convocation Detail Report" maps two more
fields off the real file: Email Id and Mobile No. Both are optional (the real file has 589 blank
emails out of 1,201 rows) and neither is validated beyond "optional text" — there is no format
check here, matching how `awards` and `seat_no` are already handled.

Both are master data in exactly the sense the rest of `students` already is: once a student is
frozen, they can only be corrected through a logged master patch, not a stray UPDATE. Migration
0010's guard function is therefore redefined (`CREATE OR REPLACE`, not a new trigger) with the two
new columns added to the comparison it makes — the triggers that call it do not need to change, only
what they compare.

Revision ID: 0011_email_mobile
Revises: 0010_master_data_guard
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_email_mobile"
down_revision: Union[str, Sequence[str], None] = "0010_master_data_guard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors migration 0010's own list, plus the two columns this migration adds.
_OLD_MASTER_COLUMNS = ["prn", "name", "programme", "school", "photo_path", "awards", "sequence_no", "seat_no", "status"]
_NEW_MASTER_COLUMNS = _OLD_MASTER_COLUMNS + ["email", "mobile"]


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
    op.execute("ALTER TABLE students ADD COLUMN email text")
    op.execute("ALTER TABLE students ADD COLUMN mobile text")
    op.execute(_guard_function_sql(_NEW_MASTER_COLUMNS))


def downgrade() -> None:
    op.execute(_guard_function_sql(_OLD_MASTER_COLUMNS))
    op.execute("ALTER TABLE students DROP COLUMN IF EXISTS mobile")
    op.execute("ALTER TABLE students DROP COLUMN IF EXISTS email")

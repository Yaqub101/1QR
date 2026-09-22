"""no sequence numbers, no seats, and a lock on frozen master data

TWO CHANGES, both driven by what the university actually sent.

1. THE REAL LIST HAS NEITHER COLUMN.
   There is no Convocation Sequence Number and no Seat Number in the data, and there may never
   be. `students.sequence_no` therefore becomes nullable and loses its UNIQUE constraint: the
   column stays so the number can be stored on the day the university supplies one, but nothing
   in the system may require it, order by it or collide on it. `seat_no` was already nullable and
   is now simply unused — Seating becomes a plain "this student is seated" confirmation.

   The CHECK that a sequence number is positive is kept: a NULL passes a CHECK, so it still only
   says "if there is a number, it is a real one".

2. FROZEN MASTER DATA IS LOCKED TO ONE DOOR.
   docs/TODO.md Phase 3: *"'Freeze display data' action fills `display_snapshot`; after freeze,
   changes only via a logged 'master patch'."* That rule is enforced here rather than only in
   application code, in the same way the Stage Controller owns the LED (migration 0006): the
   sanctioned paths set `app.master_patch = 'on'` for their own transaction, and the trigger
   refuses everything else. So after the freeze:

     * an UPDATE of a frozen student's master row is refused unless it came through the patch;
     * any UPDATE or DELETE of `display_snapshot` is refused unless it came through the freeze
       or the patch.

   A student who has not been frozen yet is untouched by this: that student is still ordinary
   import territory. Nothing here blocks INSERTs, so importing new students and freezing them
   works exactly as before.

Revision ID: 0010_master_data_guard
Revises: 0009_scan_log_ready
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_master_data_guard"
down_revision: Union[str, Sequence[str], None] = "0009_scan_log_ready"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The columns the freeze copies from and the passes print: changing one of these after the freeze is
# what the patch exists for. `prn` is in the list because it is the identity the import matches on
# and must never drift; the patch refuses to change it at all.
MASTER_COLUMNS = ["prn", "name", "programme", "school", "photo_path", "awards", "sequence_no", "seat_no", "status"]


def upgrade() -> None:
    # ---- 1. the sequence number becomes optional and repeatable -------------
    op.execute("ALTER TABLE students ALTER COLUMN sequence_no DROP NOT NULL")
    op.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_sequence_no_key")

    # ---- 2. the lock on frozen master data ---------------------------------
    new = ", ".join(f"NEW.{c}" for c in MASTER_COLUMNS)
    old = ", ".join(f"OLD.{c}" for c in MASTER_COLUMNS)
    op.execute(
        f"""
        CREATE FUNCTION master_data_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'TRUNCATE' THEN
                RAISE EXCEPTION 'display data is frozen: it cannot be emptied'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            -- The freeze and the master patch announce themselves for the length of their own
            -- transaction (SET LOCAL). Nothing else can.
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

            -- students: only a student whose display data has been frozen is locked.
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
    )
    op.execute(
        "CREATE TRIGGER students_master_data_guard BEFORE UPDATE ON students "
        "FOR EACH ROW EXECUTE FUNCTION master_data_guard()"
    )
    op.execute(
        "CREATE TRIGGER display_snapshot_guard_row BEFORE UPDATE OR DELETE ON display_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION master_data_guard()"
    )
    op.execute(
        "CREATE TRIGGER display_snapshot_guard_truncate BEFORE TRUNCATE ON display_snapshot "
        "FOR EACH STATEMENT EXECUTE FUNCTION master_data_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS display_snapshot_guard_truncate ON display_snapshot")
    op.execute("DROP TRIGGER IF EXISTS display_snapshot_guard_row ON display_snapshot")
    op.execute("DROP TRIGGER IF EXISTS students_master_data_guard ON students")
    op.execute("DROP FUNCTION IF EXISTS master_data_guard()")
    # Going back needs every student to have a sequence number again, and no two the same. That is a
    # data question, not a schema one, so it is left to whoever runs the downgrade: the statements
    # below will fail loudly rather than invent numbers.
    op.execute("ALTER TABLE students ALTER COLUMN sequence_no SET NOT NULL")
    op.execute("ALTER TABLE students ADD CONSTRAINT students_sequence_no_key UNIQUE (sequence_no)")

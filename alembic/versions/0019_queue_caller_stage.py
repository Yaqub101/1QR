"""0019_queue_caller_stage_separation: faculty mapping, queue called_at & staged_at, and LISTEN/NOTIFY trigger

1. Add `programme_faculty` table to store programme-to-faculty mapping.
2. Seed `programme_faculty` from `backend.faculty_map.DEFAULT_PROGRAMME_MAP`.
3. Add `faculty` column to `students` (with CHECK constraint) and backfill with ERP school as primary.
4. Add `called_at` and `staged_at` independent nullable timestamps to `queue`.
5. Add PostgreSQL `LISTEN/NOTIFY` trigger on `queue` (channel 'queue_events') for multi-worker SSE.

Revision ID: 0019_queue_caller_stage_separation
Revises: 0018_caller_dismissals
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from backend.faculty_map import DEFAULT_PROGRAMME_MAP, normalize_name

revision: str = "0019_queue_caller_stage"
down_revision: Union[str, Sequence[str], None] = "0018_caller_dismissals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FACULTIES = (
    "'SCIENCE'", "'ENGINEERING'", "'MANAGEMENT'", "'SOCIAL_SCI'",
    "'DESIGN'", "'INTERDISCIPLINARY'", "'PERFORMING_ARTS'", "'UNMAPPED'"
)
FACULTIES_LIST = ", ".join(FACULTIES)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. programme_faculty table
    op.execute(
        f"""
        CREATE TABLE programme_faculty (
            programme_key  TEXT PRIMARY KEY,
            programme_name TEXT NOT NULL,
            faculty        TEXT NOT NULL,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT programme_faculty_valid CHECK (faculty IN ({FACULTIES_LIST}))
        )
        """
    )

    # 2. Seed programme_faculty from DEFAULT_PROGRAMME_MAP
    # Insert each baseline mapping
    for prog_key, faculty in DEFAULT_PROGRAMME_MAP.items():
        # Store key and initial display name
        op.execute(
            sa.text(
                "INSERT INTO programme_faculty (programme_key, programme_name, faculty) "
                "VALUES (:k, :n, :f) ON CONFLICT (programme_key) DO NOTHING"
            ).bindparams(k=prog_key, n=prog_key.title(), f=faculty)
        )

    # 3. Add faculty column to students
    op.execute(
        f"""
        ALTER TABLE students ADD COLUMN faculty TEXT NOT NULL DEFAULT 'UNMAPPED';
        ALTER TABLE students ADD CONSTRAINT students_faculty_valid CHECK (faculty IN ({FACULTIES_LIST}));
        """
    )

    # 4. Backfill students.faculty
    # ERP school is the primary source of truth
    op.execute("UPDATE students SET faculty = 'SCIENCE' WHERE school = 'Basic and Applied Sciences'")
    op.execute("UPDATE students SET faculty = 'ENGINEERING' WHERE school IN ('Engineering & Technology', 'Faculty of Engineering and Technology')")
    op.execute("UPDATE students SET faculty = 'MANAGEMENT' WHERE school = 'Management and Commerce'")
    op.execute("UPDATE students SET faculty = 'SOCIAL_SCI' WHERE school = 'Social Sciences and Humanities'")
    op.execute("UPDATE students SET faculty = 'DESIGN' WHERE school = 'Faculty of Design'")
    op.execute("UPDATE students SET faculty = 'INTERDISCIPLINARY' WHERE school = 'Faculty of Interdisciplinary Studies'")
    op.execute("UPDATE students SET faculty = 'PERFORMING_ARTS' WHERE school = 'Faculty of Performing Arts'")

    # Fallback to programme_faculty for any students still UNMAPPED
    if is_postgres:
        op.execute(
            """
            UPDATE students s
            SET faculty = pf.faculty
            FROM programme_faculty pf
            WHERE s.faculty = 'UNMAPPED'
              AND regexp_replace(lower(trim(s.programme)), '\\s+', ' ', 'g') = pf.programme_key
            """
        )

    # 5. Add called_at and staged_at to queue
    op.execute("ALTER TABLE queue ADD COLUMN called_at TIMESTAMPTZ NULL")
    op.execute("ALTER TABLE queue ADD COLUMN staged_at TIMESTAMPTZ NULL")
    op.execute("CREATE INDEX queue_called_at_idx ON queue (called_at)")
    op.execute("CREATE INDEX queue_staged_at_idx ON queue (staged_at)")

    # 6. PostgreSQL trigger for queue LISTEN/NOTIFY
    if is_postgres:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION notify_queue_change() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                payload text;
            BEGIN
                payload := json_build_object(
                    'action', TG_OP,
                    'student_id', NEW.student_id,
                    'queue_position', NEW.queue_position,
                    'called_at', NEW.called_at,
                    'staged_at', NEW.staged_at,
                    'status', NEW.status
                )::text;
                PERFORM pg_notify('queue_events', payload);
                RETURN NEW;
            END;
            $$;

            DROP TRIGGER IF EXISTS queue_notify_trigger ON queue;
            CREATE TRIGGER queue_notify_trigger
                AFTER INSERT OR UPDATE ON queue
                FOR EACH ROW
                EXECUTE FUNCTION notify_queue_change();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute("DROP TRIGGER IF EXISTS queue_notify_trigger ON queue")
        op.execute("DROP FUNCTION IF EXISTS notify_queue_change()")

    op.execute("DROP INDEX IF EXISTS queue_staged_at_idx")
    op.execute("DROP INDEX IF EXISTS queue_called_at_idx")
    op.execute("ALTER TABLE queue DROP COLUMN IF EXISTS staged_at")
    op.execute("ALTER TABLE queue DROP COLUMN IF EXISTS called_at")

    op.execute("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_faculty_valid")
    op.execute("ALTER TABLE students DROP COLUMN IF EXISTS faculty")

    op.execute("DROP TABLE IF EXISTS programme_faculty")

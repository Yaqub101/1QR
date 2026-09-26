"""remove stage and led

Revision ID: a9d7eb16a3e4
Revises: 0023_student_sr_no
Create Date: 2026-09-25 21:21:26.000000

The Stage operator and the public LED are gone: the degree is handed over with no digital record, and
the Robe Return opens once the student is queued.

* stage_state is dropped, and queue.staged_at with it.
* STAGE is no longer an activity or a role. History is append-only (golden rule 5), so any STAGE events,
  scan_log rows or exceptions already recorded are KEPT: the new CHECKs are added NOT VALID, which refuses
  every new STAGE row without touching the old ones. The student_status view simply ignores them.
* SKIP was the Stage's own kind; it is now refused for every activity (again NOT VALID, old rows kept).
* A STAGE account becomes an inactive CALLER (read-only) so it can never sign in with a writing role.

Downgrade puts the constraints, view and step numbers back, but recreates stage_state only as a bare
single row: the Stage Controller's lock columns and guards (0006, 0022) are not rebuilt. Running the old
Stage code again needs a restore from backup, not a downgrade.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = 'a9d7eb16a3e4'
down_revision: Union[str, Sequence[str], None] = '0023_student_sr_no'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES_BEFORE = "'ADMIN','DEPUTY_ADMIN','REGISTRY','SEATING','QUEUE','STAGE','LUNCH','CALLER'"
ROLES_AFTER = "'ADMIN','DEPUTY_ADMIN','REGISTRY','SEATING','QUEUE','LUNCH','CALLER'"

# MONEY_* left the running flow in 0020 but stay valid for their history; only STAGE goes here.
ACTIVITIES_BEFORE = ("'REGISTRATION','THOBE_ALLOCATION','MONEY_RECEIVED','SEATING','QUEUE','STAGE','THOBE_RETURN',"
                     "'MONEY_RETURNED','LUNCH'")
ACTIVITIES_AFTER = ("'REGISTRATION','THOBE_ALLOCATION','MONEY_RECEIVED','SEATING','QUEUE','THOBE_RETURN',"
                    "'MONEY_RETURNED','LUNCH'")

# student_status view: after removing STAGE, queued → ROBE NOT RETURNED directly
_VIEW_AFTER = """
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
               coalesce(bool_or(a.activity = 'THOBE_RETURN'), false)     AS robe_back,
               coalesce(bool_or(a.activity = 'LUNCH'), false)            AS lunch
        FROM students s LEFT JOIN active a ON a.student_id = s.id
        GROUP BY s.id, s.prn
    )
    SELECT id AS student_id,
           prn,
           (CASE
               WHEN lunch                    THEN 6
               WHEN queued AND robe_back     THEN 5
               WHEN queued                   THEN 4
               WHEN robe AND seated          THEN 3
               WHEN robe                     THEN 2
               WHEN reported                 THEN 1
               ELSE 0
           END)::smallint AS step,
           CASE
               WHEN lunch                    THEN 'EXITED'
               WHEN queued AND robe_back     THEN 'LUNCH ELIGIBLE'
               WHEN queued                   THEN 'ROBE NOT RETURNED'
               WHEN robe AND seated          THEN 'SEATED / NOT QUEUED'
               WHEN robe                     THEN 'ROBE RECEIVED / NOT QUEUED'
               WHEN reported                 THEN 'REPORTED / ROBE PENDING'
               ELSE 'REGISTERED / NOT REPORTED'
           END AS status
    FROM done
"""

# The 0020 view and the 0017 step numbers, restored on downgrade.
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

_STEP_BEFORE = """
    CREATE OR REPLACE FUNCTION activity_step(a text) RETURNS smallint
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
        SELECT CASE a
            WHEN 'REGISTRATION'     THEN 1
            WHEN 'THOBE_ALLOCATION' THEN 2
            WHEN 'MONEY_RECEIVED'   THEN 2
            WHEN 'SEATING'          THEN 3
            WHEN 'QUEUE'            THEN 4
            WHEN 'STAGE'            THEN 5
            WHEN 'THOBE_RETURN'     THEN 6
            WHEN 'MONEY_RETURNED'   THEN 6
            WHEN 'LUNCH'            THEN 7
        END::smallint
    $$
"""


def upgrade() -> None:
    # 1. The Stage Controller's single row
    op.execute("DROP TABLE IF EXISTS stage_state CASCADE")

    # 2. STAGE accounts: kept for the record, but inactive and read-only
    op.execute("UPDATE users SET role = 'CALLER', active = false WHERE role = 'STAGE'")

    # 3. SKIP was Stage-only; with Stage gone it is refused everywhere (existing rows kept)
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT IF EXISTS activity_events_skip_is_stage_only")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_no_skip CHECK (kind <> 'SKIP') NOT VALID")

    # 4. activity_t and the role list lose STAGE (existing rows kept)
    op.execute("ALTER DOMAIN activity_t DROP CONSTRAINT activity_t_check")
    op.execute(f"ALTER DOMAIN activity_t ADD CONSTRAINT activity_t_check CHECK (VALUE IN ({ACTIVITIES_AFTER})) NOT VALID")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_AFTER}))")

    # 5. Update activity_owner function
    op.execute(
        """
        CREATE OR REPLACE FUNCTION activity_owner(a text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
            SELECT CASE a
                WHEN 'REGISTRATION'     THEN 'college'
                WHEN 'THOBE_ALLOCATION' THEN 'stadium'
                WHEN 'SEATING'          THEN 'stadium'
                WHEN 'QUEUE'            THEN 'stadium'
                WHEN 'THOBE_RETURN'     THEN 'hall'
                WHEN 'LUNCH'            THEN 'hall'
            END
        $$
        """
    )
    
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
                'status', NEW.status
            )::text;
            PERFORM pg_notify('queue_events', payload);
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("ALTER TABLE queue DROP COLUMN IF EXISTS staged_at")

    # 6. Update activity_step: remove STAGE (step 5), THOBE_RETURN → 5, LUNCH → 6
    op.execute("""
        CREATE OR REPLACE FUNCTION activity_step(a text) RETURNS smallint
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
            SELECT CASE a
                WHEN 'REGISTRATION'     THEN 1
                WHEN 'THOBE_ALLOCATION' THEN 2
                WHEN 'MONEY_RECEIVED'   THEN 2
                WHEN 'SEATING'          THEN 3
                WHEN 'QUEUE'            THEN 4
                WHEN 'THOBE_RETURN'     THEN 5
                WHEN 'MONEY_RETURNED'   THEN 5
                WHEN 'LUNCH'            THEN 6
            END::smallint
        $$
    """)


    # 7. Update student_status view: drop staged column, queued → ROBE NOT RETURNED
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW_AFTER.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))


def downgrade() -> None:
    op.execute("DROP VIEW student_status")
    op.execute(_VIEW_BEFORE.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
    op.execute(_STEP_BEFORE)

    # Add staged_at back to queue
    op.execute("ALTER TABLE queue ADD COLUMN staged_at timestamptz")
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
        """
    )
    
    # Recreate stage_state table
    op.execute(
        """
        CREATE TABLE stage_state (
            id                    smallint PRIMARY KEY DEFAULT 1,
            version               integer     NOT NULL DEFAULT 1,
            display_student_id    uuid REFERENCES students(id),
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT stage_state_single_row CHECK (id = 1)
        )
        """
    )
    op.execute("INSERT INTO stage_state (id) VALUES (1)")
    
    # Revert activity_owner function
    op.execute(
        """
        CREATE OR REPLACE FUNCTION activity_owner(a text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
            SELECT CASE a
                WHEN 'REGISTRATION'     THEN 'college'
                WHEN 'THOBE_ALLOCATION' THEN 'stadium'
                WHEN 'SEATING'          THEN 'stadium'
                WHEN 'QUEUE'            THEN 'stadium'
                WHEN 'STAGE'            THEN 'stadium'
                WHEN 'THOBE_RETURN'     THEN 'hall'
                WHEN 'LUNCH'            THEN 'hall'
            END
        $$
        """
    )
    
    # Revert users_role_valid constraint
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_BEFORE}))")
    
    # Revert activity_t domain
    op.execute("ALTER DOMAIN activity_t DROP CONSTRAINT activity_t_check")
    op.execute(f"ALTER DOMAIN activity_t ADD CONSTRAINT activity_t_check CHECK (VALUE IN ({ACTIVITIES_BEFORE}))")

    # Revert SKIP to Stage-only
    op.execute("ALTER TABLE activity_events DROP CONSTRAINT IF EXISTS activity_events_no_skip")
    op.execute("ALTER TABLE activity_events ADD CONSTRAINT activity_events_skip_is_stage_only "
               "CHECK (kind <> 'SKIP' OR activity = 'STAGE') NOT VALID")

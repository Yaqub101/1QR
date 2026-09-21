"""stage_state: what the public LED shows and which laptop controls the stage (Phase 11)

* stage_state is ONE row (id = 1), so "only one controller at a time" is true by construction.
  current_student_id  the student on stage, awaiting COMPLETE or SKIP
  display_student_id  the student the LED shows (NULL = the holding screen). The LED reads the approved
                      display_snapshot row of this student and nothing else (golden rule 10)
  previous_student_id the last student to leave the stage
  controller_*        the one active Stage Controller session; controller_epoch counts take-overs
  version             bumped by trigger on EVERY change, so the SSE streams can never miss one
* A trigger refuses any change that does not come from the Stage Controller (it sets
  app.stage_controller = 'on' for its own transaction): no other endpoint or service can move the LED
  (golden rule 9), even by mistake in a later phase.
* display_snapshot.led_key is an opaque random key used in the PUBLIC photo URL, so no student id or PRN
  ever appears in anything the audience screen sees.
* stage_state is local to the Stadium and is not synced (it is not an event).

Revision ID: 0006_stage_state
Revises: 0005_auth_sessions_stations
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_stage_state"
down_revision: Union[str, Sequence[str], None] = "0005_auth_sessions_stations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE display_snapshot
            ADD COLUMN led_key text NOT NULL DEFAULT substr(md5(random()::text || clock_timestamp()::text), 1, 16),
            ADD CONSTRAINT display_snapshot_led_key_key UNIQUE (led_key)
        """
    )
    op.execute(
        """
        CREATE TABLE stage_state (
            id                    smallint PRIMARY KEY DEFAULT 1,
            current_student_id    uuid REFERENCES students(id),
            display_student_id    uuid REFERENCES students(id),
            previous_student_id   uuid REFERENCES students(id),
            controller_session_id uuid,      -- no FK: session housekeeping must never touch this row
            controller_station_id text,
            controller_since      timestamptz,
            controller_epoch      bigint      NOT NULL DEFAULT 0,
            version               bigint      NOT NULL DEFAULT 0,
            updated_at            timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT stage_state_single_row CHECK (id = 1)
        )
        """
    )
    op.execute("INSERT INTO stage_state (id) VALUES (1)")
    op.execute(
        """
        CREATE FUNCTION stage_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF current_setting('app.stage_controller', true) IS DISTINCT FROM 'on' THEN
                    RAISE EXCEPTION 'stage state can only be changed by the Stage Controller'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                NEW.version := OLD.version + 1;
                NEW.updated_at := now();
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'stage state is permanent: % is not permitted', TG_OP USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER stage_state_guard_row BEFORE UPDATE OR DELETE ON stage_state FOR EACH ROW EXECUTE FUNCTION stage_state_guard()")
    op.execute("CREATE TRIGGER stage_state_guard_truncate BEFORE TRUNCATE ON stage_state FOR EACH STATEMENT EXECUTE FUNCTION stage_state_guard()")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stage_state")
    op.execute("DROP FUNCTION IF EXISTS stage_state_guard()")
    op.execute("ALTER TABLE display_snapshot DROP CONSTRAINT IF EXISTS display_snapshot_led_key_key, DROP COLUMN IF EXISTS led_key")

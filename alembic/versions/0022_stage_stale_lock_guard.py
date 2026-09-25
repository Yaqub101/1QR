"""0022_stage_stale_lock_guard: allow taking over stale Stage Controller locks

Updates stage_state_guard() trigger to permit taking over the Stage Controller lock when:
1. No controller is recorded (controller_session_id IS NULL)
2. The current controller has become stale (now() - controller_since > interval '30 seconds')
3. The requesting session is already the controller
4. Admin emergency override is active (app.stage_override = 'on')

Also avoids bumping stage_state version on heartbeat-only timestamp updates to eliminate
unnecessary SSE push traffic to the audience LED.

Revision ID: 0022_stage_stale_lock_guard
Revises: 0021_programme_faculty_override
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0022_stage_stale_lock_guard"
down_revision: Union[str, Sequence[str], None] = "0021_programme_faculty_override"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION stage_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            req_session uuid;
            is_controller_change boolean;
            is_stale boolean;
            is_admin_override boolean;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                -- Invariant: Must be marked as stage controller transaction (Golden Rule 9)
                IF current_setting('app.stage_controller', true) IS DISTINCT FROM 'on' THEN
                    RAISE EXCEPTION 'stage state can only be changed by the Stage Controller'
                        USING ERRCODE = 'restrict_violation';
                END IF;

                -- Extract requesting session from session context if provided
                BEGIN
                    req_session := nullif(current_setting('app.stage_session_id', true), '')::uuid;
                EXCEPTION WHEN OTHERS THEN
                    req_session := NULL;
                END;

                is_admin_override := (current_setting('app.stage_override', true) = 'on');

                -- Determine if controller lock is being acquired, transferred, or released
                is_controller_change := (NEW.controller_session_id IS DISTINCT FROM OLD.controller_session_id);

                -- Lock is considered stale if unheld or controller_since is older than 30s
                is_stale := (OLD.controller_session_id IS NULL)
                            OR (OLD.controller_since IS NULL)
                            OR (now() - OLD.controller_since > interval '30 seconds');

                IF is_controller_change THEN
                    IF NEW.controller_session_id IS NOT NULL THEN
                        -- Controller acquisition / takeover:
                        -- Allowed if lock is unheld, stale (>30s), held by requesting session, or admin override.
                        IF NOT (is_stale OR is_admin_override OR (req_session IS NOT NULL AND req_session = OLD.controller_session_id)) THEN
                            RAISE EXCEPTION 'stage state can only be changed by the Stage Controller'
                                USING ERRCODE = 'restrict_violation';
                        END IF;
                    ELSE
                        -- Clean release: NEW.controller_session_id IS NULL.
                        -- Allowed if releasing session is the current controller, lock is already stale/null,
                        -- or admin override / housekeeping (req_session is null).
                        IF NOT (
                            is_admin_override
                            OR is_stale
                            OR (req_session IS NOT NULL AND req_session = OLD.controller_session_id)
                            OR (req_session IS NULL)
                        ) THEN
                            RAISE EXCEPTION 'stage state can only be changed by the Stage Controller'
                                USING ERRCODE = 'restrict_violation';
                        END IF;
                    END IF;
                ELSE
                    -- Normal stage operation (NEXT, SKIP, HOME, PREVIOUS, DISPLAY, HEARTBEAT):
                    -- The lock holder is not changing.
                    -- If a controller is recorded, verify the requesting session matches if session context was provided.
                    IF OLD.controller_session_id IS NOT NULL AND req_session IS NOT NULL THEN
                        IF req_session IS DISTINCT FROM OLD.controller_session_id AND NOT is_admin_override THEN
                            RAISE EXCEPTION 'stage state can only be changed by the Stage Controller'
                                USING ERRCODE = 'restrict_violation';
                        END IF;
                    END IF;
                END IF;

                -- Bump version for SSE stream consumers whenever user-visible stage state or controller changes,
                -- but avoid bumping version when only controller_since (heartbeat) was updated to reduce unnecessary traffic.
                IF (OLD.current_student_id, OLD.display_student_id, OLD.previous_student_id, OLD.controller_session_id, OLD.controller_epoch)
                   IS DISTINCT FROM
                   (NEW.current_student_id, NEW.display_student_id, NEW.previous_student_id, NEW.controller_session_id, NEW.controller_epoch) THEN
                    NEW.version := OLD.version + 1;
                END IF;

                NEW.updated_at := now();
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'stage state is permanent: % is not permitted', TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION stage_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
        END;
        $$;
        """
    )

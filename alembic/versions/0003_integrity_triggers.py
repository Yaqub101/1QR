"""integrity triggers: append-only, gap-free counters, reversal rules (Phase 2)

Triggers rather than REVOKE: the application connects as the table owner (and in
the Docker setup as the database superuser), and REVOKE does not bind an owner.
A trigger fires for every role, so history stays append-only even against a
buggy or hostile statement that bypasses the API. (Role separation for the app
user is a Phase 17 hardening step on top of this, not a substitute.)

Revision ID: 0003_integrity_triggers
Revises: 0002_core_schema
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_integrity_triggers"
down_revision: Union[str, Sequence[str], None] = "0002_core_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APPEND_ONLY_TABLES = ["activity_events", "audit_log", "scan_log"]
UPDATED_AT_TABLES = ["students", "users", "stations", "queue", "sync_state", "settings"]


def upgrade() -> None:
    # ---------------------------------------------------------------- helpers
    op.execute(
        """
        CREATE FUNCTION forbid_modification() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not permitted', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    for table in UPDATED_AT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_touch_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
        )

    # ------------------------------------------------- append-only history
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
        )

    # ------------------------------------------------ gap-free counters
    # A counter is a row that is incremented inside the SAME transaction as the
    # insert it numbers. The row lock serialises writers per counter, and a
    # rollback undoes the increment, so numbers are strictly monotonic, unique
    # and gap-free. (A PostgreSQL SEQUENCE is NOT used: sequences are not
    # transactional, so a rolled-back insert would leave a gap that Phase 14
    # would then report as a false SEQ_GAP.)
    op.execute(
        """
        CREATE FUNCTION next_counter(p_name text) RETURNS bigint LANGUAGE plpgsql AS $$
        DECLARE
            v bigint;
        BEGIN
            INSERT INTO counters (name, value) VALUES (p_name, 1)
            ON CONFLICT (name) DO UPDATE SET value = counters.value + 1
            RETURNING value INTO v;
            RETURN v;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION counters_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.name IS DISTINCT FROM OLD.name OR NEW.value IS DISTINCT FROM OLD.value + 1 THEN
                    RAISE EXCEPTION 'counter % may only move forward by exactly one', OLD.name
                        USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'counters is protected: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER counters_guard_row BEFORE UPDATE OR DELETE ON counters FOR EACH ROW EXECUTE FUNCTION counters_guard()")
    op.execute("CREATE TRIGGER counters_guard_truncate BEFORE TRUNCATE ON counters FOR EACH STATEMENT EXECUTE FUNCTION counters_guard()")

    # ------------------------------------------------ activity_events insert rules
    op.execute(
        """
        CREATE FUNCTION activity_events_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind IN ('COMPLETE','WAIVER') AND NEW.completion_cycle > 1 THEN
                -- A later completion is only legal once the previous one was reversed.
                IF NOT EXISTS (
                    SELECT 1 FROM activity_events r
                    WHERE r.student_id = NEW.student_id AND r.activity = NEW.activity
                      AND r.kind = 'REVERSAL' AND r.completion_cycle = NEW.completion_cycle - 1
                ) THEN
                    RAISE EXCEPTION 'completion % of % is not allowed until completion % has been reversed',
                        NEW.completion_cycle, NEW.activity, NEW.completion_cycle - 1
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.kind = 'REVERSAL' THEN
                -- A reversal must point at the real completion of that student, activity and cycle.
                IF NOT EXISTS (
                    SELECT 1 FROM activity_events t
                    WHERE t.event_id = NEW.corrects_event_id
                      AND t.student_id = NEW.student_id AND t.activity = NEW.activity
                      AND t.kind IN ('COMPLETE','WAIVER') AND t.completion_cycle = NEW.completion_cycle
                ) THEN
                    RAISE EXCEPTION 'a reversal must reference an existing completion of the same student, activity and cycle'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;

            -- Locally authored events get the next per-venue number; an event replicated
            -- from another venue arrives with the number it was authored with and keeps it.
            IF NEW.venue_seq IS NULL THEN
                NEW.venue_seq := next_counter('venue_seq:' || NEW.venue_id);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER activity_events_before_insert BEFORE INSERT ON activity_events "
        "FOR EACH ROW EXECUTE FUNCTION activity_events_before_insert()"
    )

    # ------------------------------------------------ queue
    op.execute(
        """
        CREATE FUNCTION queue_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.queue_position IS NULL THEN
                NEW.queue_position := next_counter('queue_position');
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER queue_before_insert BEFORE INSERT ON queue FOR EACH ROW EXECUTE FUNCTION queue_before_insert()")
    op.execute(
        """
        CREATE FUNCTION queue_before_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.queue_position IS DISTINCT FROM OLD.queue_position
               OR NEW.student_id IS DISTINCT FROM OLD.student_id THEN
                RAISE EXCEPTION 'queue position and student are fixed once queued'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER queue_before_update BEFORE UPDATE ON queue FOR EACH ROW EXECUTE FUNCTION queue_before_update()")

    # ------------------------------------------------ qr_tokens
    op.execute(
        """
        CREATE FUNCTION qr_tokens_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.token IS DISTINCT FROM OLD.token
                   OR NEW.student_id IS DISTINCT FROM OLD.student_id
                   OR NEW.generated_at IS DISTINCT FROM OLD.generated_at THEN
                    RAISE EXCEPTION 'a QR token can never be rewritten' USING ERRCODE = 'restrict_violation';
                END IF;
                IF NOT OLD.active AND (NEW.active
                   OR NEW.deactivated_at IS DISTINCT FROM OLD.deactivated_at
                   OR NEW.deactivated_by IS DISTINCT FROM OLD.deactivated_by) THEN
                    RAISE EXCEPTION 'a deactivated QR token can never be reactivated or altered'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'QR tokens are never deleted: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER qr_tokens_guard_row BEFORE UPDATE OR DELETE ON qr_tokens FOR EACH ROW EXECUTE FUNCTION qr_tokens_guard()")
    op.execute("CREATE TRIGGER qr_tokens_guard_truncate BEFORE TRUNCATE ON qr_tokens FOR EACH STATEMENT EXECUTE FUNCTION qr_tokens_guard()")

    # ------------------------------------------------ outbox
    op.execute(
        """
        CREATE FUNCTION outbox_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.event_id IS DISTINCT FROM OLD.event_id
                   OR NEW.payload IS DISTINCT FROM OLD.payload
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'an outbox row can only be marked sent, never rewritten'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF OLD.sent_at IS NOT NULL AND NEW.sent_at IS DISTINCT FROM OLD.sent_at THEN
                    RAISE EXCEPTION 'an outbox row that was sent stays sent' USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                IF OLD.sent_at IS NULL THEN
                    RAISE EXCEPTION 'an unsent outbox row cannot be deleted' USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'outbox is protected: % is not permitted', TG_OP USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER outbox_guard_row BEFORE UPDATE OR DELETE ON outbox FOR EACH ROW EXECUTE FUNCTION outbox_guard()")
    op.execute("CREATE TRIGGER outbox_guard_truncate BEFORE TRUNCATE ON outbox FOR EACH STATEMENT EXECUTE FUNCTION outbox_guard()")


def downgrade() -> None:
    # Dropping the tables in 0002's downgrade removes their triggers; only the
    # standalone functions need dropping here (tables still exist at this point,
    # so drop the triggers explicitly first).
    for table in UPDATED_AT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table}")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute("DROP TRIGGER IF EXISTS counters_guard_row ON counters")
    op.execute("DROP TRIGGER IF EXISTS counters_guard_truncate ON counters")
    op.execute("DROP TRIGGER IF EXISTS activity_events_before_insert ON activity_events")
    op.execute("DROP TRIGGER IF EXISTS queue_before_insert ON queue")
    op.execute("DROP TRIGGER IF EXISTS queue_before_update ON queue")
    op.execute("DROP TRIGGER IF EXISTS qr_tokens_guard_row ON qr_tokens")
    op.execute("DROP TRIGGER IF EXISTS qr_tokens_guard_truncate ON qr_tokens")
    op.execute("DROP TRIGGER IF EXISTS outbox_guard_row ON outbox")
    op.execute("DROP TRIGGER IF EXISTS outbox_guard_truncate ON outbox")
    for fn in [
        "outbox_guard()",
        "qr_tokens_guard()",
        "queue_before_update()",
        "queue_before_insert()",
        "activity_events_before_insert()",
        "counters_guard()",
        "next_counter(text)",
        "touch_updated_at()",
        "forbid_modification()",
    ]:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")

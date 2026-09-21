"""sync engine tables and columns (Phases 14 + 15)

What sync needs from the database that Phase 2 did not already provide:

  sync_state    more columns. `data_as_of` is when a peer venue's data was last KNOWN current at this server:
                the cross-venue freshness rule (SYSTEM_SPEC 11.5) reads it. `last_success_at` stays "last
                successful pull".
  outbox        `attempts`, `last_error`, `rejected_at`: an event central refuses for good stops being retried
                without being falsely marked sent. (The Phase 2 guard still lets a row only be marked sent.)
  sync_log      CENTRAL ONLY, but the schema is the same everywhere. One row per event central holds, numbered
                by a gap-free counter taken inside the inserting transaction, so numbers are in COMMIT order and
                a venue's pull cursor can never skip an event that committed late. Append-only.
  sync_parked   an event that arrived before the event it depends on (a reversal before its original, cycle 2
                before the reversal of cycle 1). Kept durably, inserted the moment the dependency arrives.
                This is what makes arrival order irrelevant without loosening the Phase 2 triggers.
  conflict_events  a genuine duplicate arriving from a peer is STORED here, in full, and flagged with an
                exception; it is never merged into activity_events and never dropped. Append-only.
  venue_api_keys   per-venue keys for the sync API; only a SHA-256 of a key is stored.
  sync_meta     `epoch`: identifies one incarnation of central. A rebuilt central gets a new epoch, so venues
                restart their pull cursor instead of missing events under the new numbering.

Two partial unique indexes on `exceptions` make raising an exception idempotent (one OPEN item per event and
type; one OPEN gap per venue and first missing number).

Revision ID: 0008_sync
Revises: 0007_exceptions_guard
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_sync"
down_revision: Union[str, Sequence[str], None] = "0007_exceptions_guard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sync_state
            ADD COLUMN data_as_of       timestamptz,
            ADD COLUMN last_push_at     timestamptz,
            ADD COLUMN epoch            text,
            ADD COLUMN reported_seq     bigint,
            ADD COLUMN pending_reported integer,
            ADD COLUMN drain_total      integer NOT NULL DEFAULT 0,
            ADD COLUMN drain_done       integer NOT NULL DEFAULT 0,
            ADD CONSTRAINT sync_state_drain_valid CHECK (drain_total >= 0 AND drain_done >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE outbox
            ADD COLUMN attempts    integer NOT NULL DEFAULT 0,
            ADD COLUMN last_error  text,
            ADD COLUMN rejected_at timestamptz,
            ADD CONSTRAINT outbox_sent_xor_rejected CHECK (NOT (sent_at IS NOT NULL AND rejected_at IS NOT NULL))
        """
    )
    op.execute(
        """
        CREATE TABLE sync_log (
            central_seq  bigint      PRIMARY KEY,
            event_id     uuid        NOT NULL,   -- no FK on purpose: an FK would change how TRUNCATE of activity_events is refused (0A000, before the Phase 2 trigger)
            venue_id     venue_id_t  NOT NULL,
            received_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT sync_log_event_key UNIQUE (event_id),
            CONSTRAINT sync_log_seq_positive CHECK (central_seq > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_parked (
            event_id      uuid        PRIMARY KEY,
            venue_id      venue_id_t  NOT NULL,
            venue_seq     bigint      NOT NULL,
            payload       jsonb       NOT NULL,
            reason        text        NOT NULL,
            source        text,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            attempts      integer     NOT NULL DEFAULT 1
        )
        """
    )
    op.execute("CREATE INDEX sync_parked_venue_seq_idx ON sync_parked (venue_id, venue_seq)")
    op.execute(
        """
        CREATE TABLE conflict_events (
            id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_id          uuid        NOT NULL,
            venue_id          venue_id_t  NOT NULL,
            student_id        uuid,
            activity          activity_t,
            kind              text,
            venue_seq         bigint,
            payload           jsonb       NOT NULL,
            existing_event_id uuid,
            reason            text        NOT NULL,
            source            text,
            received_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT conflict_events_event_key UNIQUE (event_id)
        )
        """
    )
    for table in ("sync_log", "conflict_events"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
        )
    op.execute(
        """
        CREATE TABLE venue_api_keys (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            venue_id    venue_id_t  NOT NULL,
            key_hash    text        NOT NULL,
            note        text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            revoked_at  timestamptz,
            CONSTRAINT venue_api_keys_hash_key UNIQUE (key_hash)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX venue_api_keys_one_active ON venue_api_keys (venue_id) WHERE revoked_at IS NULL")
    op.execute(
        """
        CREATE FUNCTION venue_api_keys_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.key_hash IS DISTINCT FROM OLD.key_hash OR NEW.venue_id IS DISTINCT FROM OLD.venue_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                   OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                    RAISE EXCEPTION 'an API key can only be revoked, never altered' USING ERRCODE = 'restrict_violation';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'API keys are never deleted: % is not permitted', TG_OP USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute("CREATE TRIGGER venue_api_keys_guard_row BEFORE UPDATE OR DELETE ON venue_api_keys FOR EACH ROW EXECUTE FUNCTION venue_api_keys_guard()")
    op.execute("CREATE TRIGGER venue_api_keys_guard_truncate BEFORE TRUNCATE ON venue_api_keys FOR EACH STATEMENT EXECUTE FUNCTION venue_api_keys_guard()")
    op.execute(
        """
        CREATE TABLE sync_meta (
            key         text        PRIMARY KEY,
            value       text        NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("INSERT INTO sync_meta (key, value) VALUES ('epoch', gen_random_uuid()::text)")
    op.execute(
        """
        CREATE UNIQUE INDEX exceptions_open_per_event ON exceptions (type, event_id)
            WHERE status = 'OPEN' AND event_id IS NOT NULL
              AND type IN ('PROVISIONAL_UNCONFIRMED', 'CONFLICT', 'SYNC_REJECTED')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX exceptions_open_seq_gap ON exceptions (venue_id, (details->>'first_missing'))
            WHERE status = 'OPEN' AND type = 'SEQ_GAP'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS exceptions_open_seq_gap")
    op.execute("DROP INDEX IF EXISTS exceptions_open_per_event")
    op.execute("DROP TABLE IF EXISTS sync_meta")
    op.execute("DROP TRIGGER IF EXISTS venue_api_keys_guard_row ON venue_api_keys")
    op.execute("DROP TRIGGER IF EXISTS venue_api_keys_guard_truncate ON venue_api_keys")
    op.execute("DROP TABLE IF EXISTS venue_api_keys")
    op.execute("DROP FUNCTION IF EXISTS venue_api_keys_guard()")
    op.execute("DROP TABLE IF EXISTS conflict_events")
    op.execute("DROP TABLE IF EXISTS sync_parked")
    op.execute("DROP TABLE IF EXISTS sync_log")
    op.execute("ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_sent_xor_rejected, DROP COLUMN IF EXISTS rejected_at, "
               "DROP COLUMN IF EXISTS last_error, DROP COLUMN IF EXISTS attempts")
    op.execute(
        "ALTER TABLE sync_state DROP CONSTRAINT IF EXISTS sync_state_drain_valid, DROP COLUMN IF EXISTS drain_done, "
        "DROP COLUMN IF EXISTS drain_total, DROP COLUMN IF EXISTS pending_reported, DROP COLUMN IF EXISTS reported_seq, "
        "DROP COLUMN IF EXISTS epoch, DROP COLUMN IF EXISTS last_push_at, DROP COLUMN IF EXISTS data_as_of"
    )

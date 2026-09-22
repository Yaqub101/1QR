"""single-server pivot: drop venue ownership, sync engine, and station binding

docs/ARCHITECTURE_PIVOT.md (final): one shared server, no offline mode. The operator's
ROLE decides which activity they may perform, from any browser, not a physically bound
station. This migration removes everything that existed only to make three separate
venue servers agree with each other and with a central server:

  * the `stations` table (device-token binding, one station <-> one venue <-> one
    activity for life) and its identity-guard trigger
  * `sessions.station_id` (a session no longer belongs to a bound station)
  * the sync engine's tables: `outbox`, `sync_state`, `sync_log`, `sync_parked`,
    `conflict_events`, `venue_api_keys`, `sync_meta`
  * `venue_id` / `venue_seq` on `activity_events`, `scan_log`, `audit_log`; `venue_id`
    on `exceptions`; the two partial unique indexes that made CONFLICT/SEQ_GAP/
    PROVISIONAL_UNCONFIRMED/SYNC_REJECTED exceptions idempotent (their event types are
    gone with the sync engine)
  * `station_id` on `activity_events` / `scan_log` (no more station identity at all)
  * `settings.freshness_window_seconds` (the cross-venue freshness rule is gone: every
    prerequisite is now a same-server, always-current check)
  * the `activity_owner()` function and `venue_id_t` domain (the single-writer-per-venue
    rule they enforced no longer exists)

What is KEPT, unchanged: the `activity_t` domain (activities themselves are unchanged),
every append-only/immutability trigger on the tables that remain, and -- most
importantly -- the per-activity duplicate-prevention unique indexes
(`activity_events_one_completion`, `activity_events_one_reversal`). That rule was never
about venues and this migration does not touch it.

`activity_events_before_insert()` and `exceptions_guard()` are redefined (CREATE OR
REPLACE, not dropped) with the venue/venue_seq references removed from their bodies;
the triggers that call them are untouched.

Revision ID: 0012_single_server_pivot
Revises: 0011_email_mobile
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012_single_server_pivot"
down_revision: Union[str, Sequence[str], None] = "0011_email_mobile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- sync engine tables (Phase 14/15), most-dependent first ----
    op.execute("DROP TABLE IF EXISTS sync_meta")
    op.execute("DROP TRIGGER IF EXISTS venue_api_keys_guard_row ON venue_api_keys")
    op.execute("DROP TRIGGER IF EXISTS venue_api_keys_guard_truncate ON venue_api_keys")
    op.execute("DROP TABLE IF EXISTS venue_api_keys")
    op.execute("DROP FUNCTION IF EXISTS venue_api_keys_guard()")
    op.execute("DROP TRIGGER IF EXISTS conflict_events_no_update_delete ON conflict_events")
    op.execute("DROP TRIGGER IF EXISTS conflict_events_no_truncate ON conflict_events")
    op.execute("DROP TABLE IF EXISTS conflict_events")
    op.execute("DROP TABLE IF EXISTS sync_parked")
    op.execute("DROP TRIGGER IF EXISTS sync_log_no_update_delete ON sync_log")
    op.execute("DROP TRIGGER IF EXISTS sync_log_no_truncate ON sync_log")
    op.execute("DROP TABLE IF EXISTS sync_log")

    # The two partial unique indexes existed only to make sync/reconciliation exceptions
    # idempotent; those exception types (CONFLICT, SEQ_GAP, PROVISIONAL_UNCONFIRMED,
    # SYNC_REJECTED) no longer occur anywhere in the application.
    op.execute("DROP INDEX IF EXISTS exceptions_open_seq_gap")
    op.execute("DROP INDEX IF EXISTS exceptions_open_per_event")

    op.execute("DROP TRIGGER IF EXISTS outbox_guard_row ON outbox")
    op.execute("DROP TRIGGER IF EXISTS outbox_guard_truncate ON outbox")
    op.execute("DROP TABLE IF EXISTS outbox")
    op.execute("DROP FUNCTION IF EXISTS outbox_guard()")
    op.execute("DROP TABLE IF EXISTS sync_state")

    # ---- stations: the device-token binding and its identity guard ----
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS station_id")
    op.execute("DROP TRIGGER IF EXISTS stations_identity_guard ON stations")
    op.execute("DROP FUNCTION IF EXISTS stations_identity_guard()")
    op.execute("DROP TABLE IF EXISTS stations")

    # ---- activity_events: drop venue/venue_seq/station_id, redefine the insert trigger ----
    op.execute(
        """
        CREATE OR REPLACE FUNCTION activity_events_before_insert() RETURNS trigger LANGUAGE plpgsql AS $$
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
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE activity_events
            DROP CONSTRAINT IF EXISTS activity_events_venue_seq_key,
            DROP CONSTRAINT IF EXISTS activity_events_owned_by_venue,
            DROP CONSTRAINT IF EXISTS activity_events_venue_seq_positive,
            DROP CONSTRAINT IF EXISTS activity_events_station_required,
            DROP COLUMN IF EXISTS venue_id,
            DROP COLUMN IF EXISTS venue_seq,
            DROP COLUMN IF EXISTS station_id
        """
    )

    # ---- scan_log: drop venue_id, station_id ----
    op.execute("ALTER TABLE scan_log DROP COLUMN IF EXISTS venue_id, DROP COLUMN IF EXISTS station_id")

    # ---- exceptions: drop venue_id, redefine the guard (it referenced venue_id) ----
    op.execute(
        """
        CREATE OR REPLACE FUNCTION exceptions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status = 'RESOLVED' THEN
                    RAISE EXCEPTION 'a resolved exception is final' USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.type IS DISTINCT FROM OLD.type
                   OR NEW.student_id IS DISTINCT FROM OLD.student_id
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
    op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS venue_id")

    # ---- audit_log: drop venue_id, venue_seq, station_id ----
    op.execute(
        "ALTER TABLE audit_log DROP COLUMN IF EXISTS venue_id, DROP COLUMN IF EXISTS venue_seq, "
        "DROP COLUMN IF EXISTS station_id"
    )

    # ---- settings: drop the cross-venue freshness window ----
    op.execute(
        "ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_freshness_positive, "
        "DROP COLUMN IF EXISTS freshness_window_seconds"
    )

    # ---- the single-writer-per-venue mapping itself ----
    op.execute("DROP FUNCTION IF EXISTS activity_owner(text)")
    op.execute("DROP DOMAIN IF EXISTS venue_id_t")


def downgrade() -> None:
    raise NotImplementedError(
        "This pivot is one-way: the venue/sync/station data it removes cannot be reconstructed "
        "from what a single-server deployment keeps. Restore from a pre-pivot backup instead."
    )

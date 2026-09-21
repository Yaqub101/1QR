"""core schema: tables, constraints and indexes (Phase 2)

Every rule that PostgreSQL can enforce is enforced here or in 0003 (triggers),
not only in application code. See docs/SYSTEM_SPEC.md sections 5, 9, 11, 15, 17.

Design notes
- Text + CHECK (via DOMAINs for venue/activity) rather than PG ENUMs: adding a
  value later is a one-line migration, not an ALTER TYPE dance.
- students.id is a UUID so the same student has the same id at every venue and
  at central (the master pack carries the ids); events reference students by it.
- activity_events keeps no FKs to users/stations: an event replicated from
  another venue must always be insertable, and the audit trail must outlive any
  change to those tables.

Revision ID: 0002_core_schema
Revises: 9ab9206b8f3f
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_core_schema"
down_revision: Union[str, Sequence[str], None] = "9ab9206b8f3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FLAG_VALUES = "ARRAY['PROVISIONAL','MANUAL','LATE','CORRECTED']::text[]"

TABLES_IN_DROP_ORDER = [
    "audit_log",
    "exceptions",
    "sync_state",
    "outbox",
    "queue",
    "scan_log",
    "activity_events",
    "qr_tokens",
    "display_snapshot",
    "stations",
    "settings",
    "counters",
    "users",
    "students",
]


def upgrade() -> None:
    # ---- reference domains & the single-writer mapping (SYSTEM_SPEC 11.2) ----
    op.execute("CREATE DOMAIN venue_id_t AS text CHECK (VALUE IN ('college','stadium','hall'))")
    op.execute(
        """
        CREATE DOMAIN activity_t AS text CHECK (VALUE IN (
            'REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'))
        """
    )
    op.execute(
        """
        CREATE FUNCTION activity_owner(a text) RETURNS text
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

    # ---- master data ----
    op.execute(
        """
        CREATE TABLE students (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            prn          text        NOT NULL,
            name         text        NOT NULL,
            programme    text        NOT NULL,
            school       text        NOT NULL,
            photo_path   text,
            awards       text,
            sequence_no  integer     NOT NULL,
            seat_no      text,
            status       text        NOT NULL DEFAULT 'ACTIVE',
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT students_prn_key          UNIQUE (prn),
            CONSTRAINT students_sequence_no_key  UNIQUE (sequence_no),
            CONSTRAINT students_prn_not_blank    CHECK (btrim(prn) <> ''),
            CONSTRAINT students_name_not_blank   CHECK (btrim(name) <> ''),
            CONSTRAINT students_sequence_no_positive CHECK (sequence_no > 0),
            CONSTRAINT students_status_valid     CHECK (status IN ('ACTIVE','INACTIVE'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE display_snapshot (
            student_id    uuid PRIMARY KEY REFERENCES students(id),
            display_name  text        NOT NULL,
            programme     text        NOT NULL,
            school        text        NOT NULL,
            award         text,
            photo_path    text,
            frozen_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ---- users / stations ----
    op.execute(
        """
        CREATE TABLE users (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            username       text        NOT NULL,
            password_hash  text        NOT NULL,
            full_name      text,
            role           text        NOT NULL,
            active         boolean     NOT NULL DEFAULT true,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT users_username_not_blank CHECK (btrim(username) <> ''),
            CONSTRAINT users_role_valid CHECK (role IN (
                'ADMIN','REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX users_username_lower_key ON users (lower(username))")
    op.execute(
        """
        CREATE TABLE stations (
            station_id  text        PRIMARY KEY,
            venue_id    venue_id_t  NOT NULL,
            activity    activity_t  NOT NULL,
            active      boolean     NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT stations_station_id_not_blank CHECK (btrim(station_id) <> ''),
            CONSTRAINT stations_activity_owned_by_venue CHECK (venue_id = activity_owner(activity))
        )
        """
    )

    # ---- QR tokens: at most one ACTIVE token per student ----
    op.execute(
        """
        CREATE TABLE qr_tokens (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id      uuid        NOT NULL REFERENCES students(id),
            token           text        NOT NULL,
            active          boolean     NOT NULL DEFAULT true,
            generated_at    timestamptz NOT NULL DEFAULT now(),
            deactivated_at  timestamptz,
            deactivated_by  uuid REFERENCES users(id),
            CONSTRAINT qr_tokens_token_key UNIQUE (token),
            CONSTRAINT qr_tokens_token_not_blank CHECK (btrim(token) <> ''),
            CONSTRAINT qr_tokens_active_state CHECK (
                (active AND deactivated_at IS NULL AND deactivated_by IS NULL)
                OR (NOT active AND deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX qr_tokens_one_active_per_student ON qr_tokens (student_id) WHERE active")

    # ---- activity_events: the append-only journey log ----
    op.execute(
        f"""
        CREATE TABLE activity_events (
            event_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            student_id         uuid        NOT NULL REFERENCES students(id),
            activity           activity_t  NOT NULL,
            kind               text        NOT NULL DEFAULT 'COMPLETE',
            venue_id           venue_id_t  NOT NULL,
            venue_seq          bigint      NOT NULL,  -- filled by trigger when omitted (see 0003)
            station_id         text,
            operator_id        uuid        NOT NULL,
            server_time        timestamptz NOT NULL DEFAULT now(),
            local_time         timestamptz,           -- operator laptop clock: informational only
            flags              text[]      NOT NULL DEFAULT '{{}}',
            details            jsonb       NOT NULL DEFAULT '{{}}',
            completion_cycle   integer     NOT NULL DEFAULT 1,
            corrects_event_id  uuid,
            CONSTRAINT activity_events_venue_seq_key UNIQUE (venue_id, venue_seq),
            CONSTRAINT activity_events_kind_valid CHECK (kind IN ('COMPLETE','SKIP','WAIVER','REVERSAL')),
            -- single-writer rule (SYSTEM_SPEC 11.2): the row's venue must own the activity
            CONSTRAINT activity_events_owned_by_venue CHECK (venue_id = activity_owner(activity)),
            CONSTRAINT activity_events_venue_seq_positive CHECK (venue_seq > 0),
            CONSTRAINT activity_events_flags_valid CHECK (flags <@ {FLAG_VALUES}),
            CONSTRAINT activity_events_details_object CHECK (jsonb_typeof(details) = 'object'),
            CONSTRAINT activity_events_cycle_positive CHECK (completion_cycle >= 1),
            CONSTRAINT activity_events_skip_is_stage_only CHECK (kind <> 'SKIP' OR activity = 'STAGE'),
            CONSTRAINT activity_events_waiver_is_return_only CHECK (kind <> 'WAIVER' OR activity = 'THOBE_RETURN'),
            CONSTRAINT activity_events_reason_required CHECK (
                kind = 'COMPLETE' OR length(btrim(coalesce(details->>'reason', ''))) > 0),
            CONSTRAINT activity_events_waiver_flagged_corrected CHECK (
                kind <> 'WAIVER' OR 'CORRECTED' = ANY (flags)),
            CONSTRAINT activity_events_reversal_references_original CHECK (
                (kind = 'REVERSAL') = (corrects_event_id IS NOT NULL)),
            CONSTRAINT activity_events_station_required CHECK (
                kind IN ('WAIVER','REVERSAL') OR station_id IS NOT NULL)
        )
        """
    )
    # THE duplicate-prevention guarantee (SYSTEM_SPEC 15): one active completion per
    # (student, activity). WAIVER shares the slot with COMPLETE. A REVERSAL re-opens the
    # slot by bumping completion_cycle; the trigger in 0003 validates that bump.
    op.execute(
        """
        CREATE UNIQUE INDEX activity_events_one_completion
            ON activity_events (student_id, activity, completion_cycle)
            WHERE kind IN ('COMPLETE','WAIVER')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX activity_events_one_reversal
            ON activity_events (student_id, activity, completion_cycle)
            WHERE kind = 'REVERSAL'
        """
    )
    op.execute("CREATE INDEX activity_events_student_activity_idx ON activity_events (student_id, activity)")

    # ---- scan_log ----
    op.execute(
        """
        CREATE TABLE scan_log (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            occurred_at     timestamptz NOT NULL DEFAULT now(),
            local_time      timestamptz,
            venue_id        venue_id_t  NOT NULL,
            station_id      text        NOT NULL,
            activity        activity_t  NOT NULL,
            operator_id     uuid,
            student_id      uuid REFERENCES students(id),
            token_presented text,
            prn_entered     text,
            result          text        NOT NULL,
            message         text,
            event_id        uuid,
            details         jsonb       NOT NULL DEFAULT '{}',
            CONSTRAINT scan_log_result_valid CHECK (result IN (
                'SUCCESS','DUPLICATE','INVALID','REJECTED','PROVISIONAL','MANUAL'))
        )
        """
    )
    op.execute("CREATE INDEX scan_log_student_idx ON scan_log (student_id)")
    op.execute("CREATE INDEX scan_log_occurred_at_idx ON scan_log (occurred_at)")

    # ---- counters (gap-free per-venue venue_seq and queue_position), queue ----
    op.execute(
        """
        CREATE TABLE counters (
            name   text   PRIMARY KEY,
            value  bigint NOT NULL,
            CONSTRAINT counters_value_non_negative CHECK (value >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE queue (
            student_id      uuid PRIMARY KEY REFERENCES students(id),
            queue_position  bigint      NOT NULL,   -- filled by trigger from the counter (see 0003)
            status          text        NOT NULL DEFAULT 'QUEUED',
            queued_at       timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT queue_position_key UNIQUE (queue_position),
            CONSTRAINT queue_position_positive CHECK (queue_position > 0),
            CONSTRAINT queue_status_valid CHECK (status IN ('QUEUED','DISPLAYED','DONE','SKIPPED','HELD'))
        )
        """
    )
    op.execute("CREATE INDEX queue_status_position_idx ON queue (status, queue_position)")

    # ---- sync ----
    op.execute(
        """
        CREATE TABLE outbox (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            event_id    uuid        NOT NULL,
            payload     jsonb       NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            sent_at     timestamptz,
            CONSTRAINT outbox_event_id_key UNIQUE (event_id)
        )
        """
    )
    op.execute("CREATE INDEX outbox_unsent_idx ON outbox (id) WHERE sent_at IS NULL")
    op.execute(
        """
        CREATE TABLE sync_state (
            peer             text PRIMARY KEY,
            cursor           bigint      NOT NULL DEFAULT 0,
            last_success_at  timestamptz,
            last_error       text,
            last_error_at    timestamptz,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT sync_state_peer_valid CHECK (peer IN ('central','college','stadium','hall')),
            CONSTRAINT sync_state_cursor_non_negative CHECK (cursor >= 0)
        )
        """
    )

    # ---- exceptions (Admin review list) ----
    op.execute(
        """
        CREATE TABLE exceptions (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            type             text        NOT NULL,
            student_id       uuid REFERENCES students(id),
            venue_id         venue_id_t,
            event_id         uuid,
            details          jsonb       NOT NULL DEFAULT '{}',
            status           text        NOT NULL DEFAULT 'OPEN',
            created_at       timestamptz NOT NULL DEFAULT now(),
            resolved_by      uuid REFERENCES users(id),
            resolved_at      timestamptz,
            resolution_note  text,
            CONSTRAINT exceptions_type_not_blank CHECK (btrim(type) <> ''),
            CONSTRAINT exceptions_status_valid CHECK (status IN ('OPEN','RESOLVED')),
            CONSTRAINT exceptions_resolution_consistent CHECK (
                (status = 'OPEN' AND resolved_at IS NULL AND resolved_by IS NULL)
                OR (status = 'RESOLVED' AND resolved_at IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX exceptions_status_type_idx ON exceptions (status, type)")

    # ---- audit_log (SYSTEM_SPEC 17). Sync time is outbox.sent_at: the row itself is immutable. ----
    op.execute(
        f"""
        CREATE TABLE audit_log (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            occurred_at        timestamptz NOT NULL DEFAULT now(),   -- server time
            local_time         timestamptz,                          -- operator laptop clock
            action             text        NOT NULL,
            student_id         uuid REFERENCES students(id),
            activity           activity_t,
            venue_id           venue_id_t,
            station_id         text,
            operator_id        uuid REFERENCES users(id),
            event_id           uuid,
            venue_seq          bigint,
            flags              text[]      NOT NULL DEFAULT '{{}}',
            corrects_event_id  uuid,
            corrected_by       uuid REFERENCES users(id),
            reason             text,
            details            jsonb       NOT NULL DEFAULT '{{}}',
            CONSTRAINT audit_log_action_not_blank CHECK (btrim(action) <> ''),
            CONSTRAINT audit_log_flags_valid CHECK (flags <@ {FLAG_VALUES}),
            CONSTRAINT audit_log_correction_has_admin CHECK (corrects_event_id IS NULL OR corrected_by IS NOT NULL),
            CONSTRAINT audit_log_correction_has_reason CHECK (
                corrected_by IS NULL OR length(btrim(coalesce(reason, ''))) > 0)
        )
        """
    )
    op.execute("CREATE INDEX audit_log_student_idx ON audit_log (student_id)")
    op.execute("CREATE INDEX audit_log_event_idx ON audit_log (event_id)")

    # ---- settings: one row ----
    op.execute(
        """
        CREATE TABLE settings (
            id                        smallint PRIMARY KEY DEFAULT 1,
            event_name                text        NOT NULL DEFAULT 'Convocation Ceremony',
            late_cutoff               timestamptz,
            freshness_window_seconds  integer     NOT NULL DEFAULT 120,
            holding_screen_text       text        NOT NULL DEFAULT '',
            updated_at                timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT settings_single_row CHECK (id = 1),
            CONSTRAINT settings_freshness_positive CHECK (freshness_window_seconds > 0)
        )
        """
    )
    op.execute("INSERT INTO settings (id) VALUES (1)")


def downgrade() -> None:
    for table in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS activity_owner(text)")
    op.execute("DROP DOMAIN IF EXISTS activity_t")
    op.execute("DROP DOMAIN IF EXISTS venue_id_t")

"""auth: deputy role, sessions, station binding (Phase 5)

* users.role gains DEPUTY_ADMIN: same powers as ADMIN (SYSTEM_SPEC section 4) but a
  separate identity, so every audited action names the individual.
* sessions: server-side sessions. Only a SHA-256 of the token is stored. Server-side
  (not signed cookies) so that disabling a user, unbinding a station or logging out
  takes effect on the very next request, with no clock or secret shared between
  laptops. Times are server times only.
* stations: a laptop is bound to a station by a random device token (stored hashed).
  One laptop <-> one station, enforced by a unique index. A station's id, venue and
  activity are fixed for life: a station is bound to exactly one activity.

Revision ID: 0005_auth_sessions_stations
Revises: 0004_student_status_view
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_auth_sessions_stations"
down_revision: Union[str, Sequence[str], None] = "0004_student_status_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES_BEFORE = "'ADMIN','REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'"
ROLES_AFTER = "'ADMIN','DEPUTY_ADMIN','REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'"


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_AFTER}))")

    op.execute(
        """
        ALTER TABLE stations
            ADD COLUMN device_token_hash text,
            ADD COLUMN bound_at          timestamptz,
            ADD COLUMN bound_by          uuid REFERENCES users(id),
            ADD CONSTRAINT stations_device_token_hash_key UNIQUE (device_token_hash),
            ADD CONSTRAINT stations_binding_complete CHECK (
                (device_token_hash IS NULL AND bound_at IS NULL AND bound_by IS NULL)
                OR (device_token_hash IS NOT NULL AND bound_at IS NOT NULL AND bound_by IS NOT NULL))
        """
    )
    op.execute(
        """
        CREATE FUNCTION stations_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.station_id IS DISTINCT FROM OLD.station_id
               OR NEW.venue_id IS DISTINCT FROM OLD.venue_id
               OR NEW.activity IS DISTINCT FROM OLD.activity THEN
                RAISE EXCEPTION 'a station stays bound to one venue and one activity for life'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER stations_identity_guard BEFORE UPDATE ON stations "
        "FOR EACH ROW EXECUTE FUNCTION stations_identity_guard()"
    )

    op.execute(
        """
        CREATE TABLE sessions (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            token_hash    text        NOT NULL,
            user_id       uuid        NOT NULL REFERENCES users(id),
            station_id    text REFERENCES stations(station_id),
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_seen_at  timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            revoked_at    timestamptz,
            CONSTRAINT sessions_token_hash_key UNIQUE (token_hash)
        )
        """
    )
    op.execute("CREATE INDEX sessions_user_idx ON sessions (user_id) WHERE revoked_at IS NULL")
    op.execute("CREATE INDEX sessions_station_idx ON sessions (station_id) WHERE revoked_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TRIGGER IF EXISTS stations_identity_guard ON stations")
    op.execute("DROP FUNCTION IF EXISTS stations_identity_guard()")
    op.execute(
        """
        ALTER TABLE stations
            DROP CONSTRAINT IF EXISTS stations_binding_complete,
            DROP CONSTRAINT IF EXISTS stations_device_token_hash_key,
            DROP COLUMN IF EXISTS bound_by,
            DROP COLUMN IF EXISTS bound_at,
            DROP COLUMN IF EXISTS device_token_hash
        """
    )
    op.execute("UPDATE users SET role = 'ADMIN' WHERE role = 'DEPUTY_ADMIN'")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_BEFORE}))")

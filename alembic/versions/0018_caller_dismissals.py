"""caller_dismissals: per-device caller queue state (Feature: caller queue list)

The Caller screen now shows all queued students (not just the current LED student). When the
caller has found a student and directed them to the stage entrance, they tap Complete on that
row. This is a UI state: it does NOT remove the student from the Stage operator's list, and the
Stage operator's NEXT / SEND do NOT remove students from the caller's list.

Separate storage, completely independent of queue.status and stage_state:

    caller_dismissals
    -----------------
    id            bigint identity PK
    student_id    uuid REFERENCES students(id)
    dismissed_by  int  REFERENCES users(id)    -- the caller operator who tapped Complete
    dismissed_at  timestamptz

UNIQUE (student_id): idempotent; a second dismiss from any device is a no-op update.

Downgrade empties the table silently (it is ephemeral UI state, not ceremony history).

A version counter row is added to the 'counters' table under the key 'caller_dismissals_v'
so the SSE stream can detect changes cheaply (one SELECT on the counters table).

Revision ID: 0018_caller_dismissals
Revises: 0017_money_deposit
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018_caller_dismissals"
down_revision: Union[str, Sequence[str], None] = "0017_money_deposit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE caller_dismissals (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            student_id    uuid        NOT NULL REFERENCES students(id),
            dismissed_by  uuid        NOT NULL REFERENCES users(id),
            dismissed_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT caller_dismissals_student_key UNIQUE (student_id)
        )
        """
    )
    op.execute("CREATE INDEX caller_dismissals_dismissed_by_idx ON caller_dismissals (dismissed_by)")
    # Seed the version counter row so the SSE can SELECT it without an INSERT.
    op.execute("INSERT INTO counters (name, value) VALUES ('caller_dismissals_v', 0) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM caller_dismissals")
    op.execute("DROP TABLE IF EXISTS caller_dismissals")
    # Note: the counters row is left in place — the counters table is append-only
    # (protected by a guard trigger that blocks DELETE). The orphan row is harmless.

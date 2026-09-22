"""scan_log: allow the READY result, so a successful scan can be logged like every other attempt

TODO.md Phase 6 requires "Every attempt written to `scan_log`". Refusals (INVALID / REJECTED /
DUPLICATE) and confirmations (SUCCESS / PROVISIONAL / MANUAL) were written; a successful SCAN was
not, because there was no result value for it and the engine simply skipped the row.

READY is what the operator sees when the card comes up: the student was identified, every rule
passed, and the operator has not pressed confirm yet. A READY row carries no `event_id`, because
nothing has been recorded — which is exactly what makes it worth keeping. It is the only evidence
that a student stood at a station at all, so "was this student ever presented here, and did the
operator walk away without confirming?" becomes a question the log can answer.

The CHECK constraint is the only thing that changes. scan_log stays append-only (0003), and no
existing row is touched: the new value is added to the vocabulary, none is removed.

Revision ID: 0009_scan_log_ready
Revises: 0008_sync
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009_scan_log_ready"
down_revision: Union[str, Sequence[str], None] = "0008_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE scan_log DROP CONSTRAINT scan_log_result_valid")
    op.execute(
        """
        ALTER TABLE scan_log ADD CONSTRAINT scan_log_result_valid CHECK (result IN (
            'READY','SUCCESS','DUPLICATE','INVALID','REJECTED','PROVISIONAL','MANUAL'))
        """
    )


def downgrade() -> None:
    # scan_log is append-only (0003), so the trigger that enforces that has to stand aside for the one
    # statement that removes the rows the older vocabulary cannot describe. It goes straight back on.
    op.execute("ALTER TABLE scan_log DISABLE TRIGGER scan_log_no_update_delete")
    op.execute("DELETE FROM scan_log WHERE result = 'READY'")
    op.execute("ALTER TABLE scan_log ENABLE TRIGGER scan_log_no_update_delete")
    op.execute("ALTER TABLE scan_log DROP CONSTRAINT scan_log_result_valid")
    op.execute(
        """
        ALTER TABLE scan_log ADD CONSTRAINT scan_log_result_valid CHECK (result IN (
            'SUCCESS','DUPLICATE','INVALID','REJECTED','PROVISIONAL','MANUAL'))
        """
    )

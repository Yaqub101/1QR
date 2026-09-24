"""0021_programme_faculty_override: add is_override to programme_faculty

Allows admin overrides of faculty assignments to take priority over ERP school.

Revision ID: 0021_programme_faculty_override
Revises: 0020_remove_money_from_flow
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021_programme_faculty_override"
down_revision: Union[str, Sequence[str], None] = "0020_remove_money_from_flow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE programme_faculty
        ADD COLUMN is_override BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE programme_faculty
        DROP COLUMN IF EXISTS is_override;
        """
    )

"""roles: a read-only CALLER (role/flow redesign, Phase R3)

The Caller reads each graduate's name aloud from the internal Caller screen, which always shows the
same student as the public LED. The role performs no activity and can change nothing: it may only
view that screen (the Stage operator and the Admins may view it too). This adds CALLER to
users_role_valid.

Downgrade refuses while any CALLER account exists: it never deletes or silently re-roles accounts.

Revision ID: 0015_caller_role
Revises: 0014_registry_role
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0015_caller_role"
down_revision: Union[str, Sequence[str], None] = "0014_registry_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES_BEFORE = "'ADMIN','DEPUTY_ADMIN','REGISTRY','SEATING','QUEUE','STAGE','LUNCH'"
ROLES_AFTER = "'ADMIN','DEPUTY_ADMIN','REGISTRY','SEATING','QUEUE','STAGE','LUNCH','CALLER'"


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_AFTER}))")


def downgrade() -> None:
    callers = op.get_bind().execute(text("SELECT count(*) FROM users WHERE role = 'CALLER'")).scalar_one()
    if callers:
        raise RuntimeError(f"{callers} CALLER account(s) exist; give them another role or remove them first")
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_BEFORE}))")

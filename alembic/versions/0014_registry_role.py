"""roles: Reporting, Robe Allocation and Robe Return operators merge into ONE Registry operator

The approved role/flow redesign has one Registry desk that meets the student on entry
(Reporting + Robe Allocation, one scan, one confirm) and again for the Robe Return. So the
three operator roles REGISTRATION, THOBE_ALLOCATION and THOBE_RETURN become one role, REGISTRY.

* Every existing account holding one of the three roles is moved to REGISTRY, and each move is
  written to the append-only audit_log as ROLE_MERGED (who, from which role, to which).
* users_role_valid then accepts only the new list, so the old roles cannot come back by accident.
* The activity codes themselves are NOT touched: REGISTRATION / THOBE_ALLOCATION / THOBE_RETURN
  are still separate activities with their own events and their own per-activity unique
  constraint. Only who may perform them changed.

Downgrade is lossy by nature: the database no longer knows which of the three old roles a
REGISTRY account used to be, so every REGISTRY account goes back to REGISTRATION (the entry desk).

Revision ID: 0014_registry_role
Revises: 0013_robe_status_labels
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014_registry_role"
down_revision: Union[str, Sequence[str], None] = "0013_robe_status_labels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES_BEFORE = "'ADMIN','DEPUTY_ADMIN','REGISTRATION','THOBE_ALLOCATION','SEATING','QUEUE','STAGE','THOBE_RETURN','LUNCH'"
ROLES_AFTER = "'ADMIN','DEPUTY_ADMIN','REGISTRY','SEATING','QUEUE','STAGE','LUNCH'"


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute(
        """
        INSERT INTO audit_log (action, details)
        SELECT 'ROLE_MERGED',
               jsonb_build_object('user_id', id, 'username', username, 'from_role', role, 'to_role', 'REGISTRY',
                                  'reason', 'Reporting, Robe Allocation and Robe Return operators merged into Registry')
        FROM users WHERE role IN ('REGISTRATION', 'THOBE_ALLOCATION', 'THOBE_RETURN')
        """
    )
    op.execute("UPDATE users SET role = 'REGISTRY' WHERE role IN ('REGISTRATION', 'THOBE_ALLOCATION', 'THOBE_RETURN')")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_AFTER}))")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT users_role_valid")
    op.execute("UPDATE users SET role = 'REGISTRATION' WHERE role = 'REGISTRY'")
    op.execute(f"ALTER TABLE users ADD CONSTRAINT users_role_valid CHECK (role IN ({ROLES_BEFORE}))")

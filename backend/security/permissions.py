"""Roles and permissions (SYSTEM_SPEC section 4).

One table decides everything. ADMIN and DEPUTY_ADMIN map to the very same
permission set, so "identical powers" is true by construction rather than by
keeping two lists in step. Endpoints never test a role name directly; they ask
`has_permission` / `can_use_activity`.
"""
from __future__ import annotations

from backend.security.ownership import ACTIVITIES

ADMIN_ROLES = ("ADMIN", "DEPUTY_ADMIN")
OPERATOR_ROLES = ACTIVITIES  # one operator role per activity, named after it
ALL_ROLES = ADMIN_ROLES + OPERATOR_ROLES

ADMIN_PERMISSION = "admin"


def activity_permission(activity: str) -> str:
    return f"activity:{activity}"


_ADMIN_PERMISSIONS = frozenset({ADMIN_PERMISSION, *(activity_permission(a) for a in ACTIVITIES)})

_PERMISSIONS: dict[str, frozenset[str]] = {role: _ADMIN_PERMISSIONS for role in ADMIN_ROLES}
_PERMISSIONS.update({role: frozenset({activity_permission(role)}) for role in OPERATOR_ROLES})


def permissions_for(role: str) -> frozenset[str]:
    return _PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def can_use_activity(role: str, activity: str) -> bool:
    return has_permission(role, activity_permission(activity))


def is_admin_role(role: str) -> bool:
    return has_permission(role, ADMIN_PERMISSION)

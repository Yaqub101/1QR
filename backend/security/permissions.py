"""Roles and permissions (SYSTEM_SPEC section 4, as changed by the approved role/flow redesign).

One table decides everything. ADMIN and DEPUTY_ADMIN map to the very same
permission set, so "identical powers" is true by construction rather than by
keeping two lists in step. Endpoints never test a role name directly; they ask
`has_permission` / `can_use_activity`.

The redesign merged the Reporting, Robe Allocation and Robe Return operators into ONE
Registry operator (REGISTRY), who meets the student at entry and again for the robe return.
The other operator roles stay one per activity. CALLER (Phase R3) performs no activity at all:
it may only view the read-only Caller screen, which the Stage operator and the Admins may view too.
"""
from __future__ import annotations

from backend.security.ownership import ACTIVITIES

ADMIN_ROLES = ("ADMIN", "DEPUTY_ADMIN")

# operator role -> the activities it may perform
OPERATOR_ROLE_ACTIVITIES: dict[str, tuple[str, ...]] = {
    "REGISTRY": ("REGISTRATION", "THOBE_ALLOCATION", "THOBE_RETURN"),
    "SEATING": ("SEATING",),
    "QUEUE": ("QUEUE",),
    "STAGE": ("STAGE",),
    "LUNCH": ("LUNCH",),
    "CALLER": (),  # read-only: the Caller screen and nothing else
}
OPERATOR_ROLES = tuple(OPERATOR_ROLE_ACTIVITIES)
ALL_ROLES = ADMIN_ROLES + OPERATOR_ROLES

ADMIN_PERMISSION = "admin"
CALLER_VIEW_PERMISSION = "view:caller"
# Screens a role may VIEW on top of its activities.
_VIEW_PERMISSIONS: dict[str, frozenset[str]] = {
    "CALLER": frozenset({CALLER_VIEW_PERMISSION}),
    "STAGE": frozenset({CALLER_VIEW_PERMISSION}),
}


def activity_permission(activity: str) -> str:
    return f"activity:{activity}"


_ADMIN_PERMISSIONS = frozenset({ADMIN_PERMISSION, CALLER_VIEW_PERMISSION, *(activity_permission(a) for a in ACTIVITIES)})

_PERMISSIONS: dict[str, frozenset[str]] = {role: _ADMIN_PERMISSIONS for role in ADMIN_ROLES}
_PERMISSIONS.update({role: frozenset(activity_permission(a) for a in acts) | _VIEW_PERMISSIONS.get(role, frozenset())
                     for role, acts in OPERATOR_ROLE_ACTIVITIES.items()})

_ROLE_FOR_ACTIVITY = {a: role for role, acts in OPERATOR_ROLE_ACTIVITIES.items() for a in acts}
assert set(_ROLE_FOR_ACTIVITY) == set(ACTIVITIES), "every activity needs exactly one operator role"


def permissions_for(role: str) -> frozenset[str]:
    return _PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    return permission in permissions_for(role)


def can_use_activity(role: str, activity: str) -> bool:
    return has_permission(role, activity_permission(activity))


def operator_role_for(activity: str) -> str:
    """The one operator role that performs `activity`."""
    return _ROLE_FOR_ACTIVITY[activity]


def is_admin_role(role: str) -> bool:
    return has_permission(role, ADMIN_PERMISSION)

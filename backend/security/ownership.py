"""The activity vocabulary shared across the server (SYSTEM_SPEC section 2).

Before the single-server pivot (docs/ARCHITECTURE_PIVOT.md) this module also enforced
venue ownership -- which of three separate servers was allowed to record which
activity. That rule is gone: there is one server, and the operator's ROLE (see
backend/security/permissions.py) decides which activity they may perform, from any
browser. What is left here is just the list of activities and their display labels,
used everywhere an activity name needs validating or showing to a person.
"""
from __future__ import annotations

ACTIVITIES = (
    "REGISTRATION",
    "THOBE_ALLOCATION",
    "SEATING",
    "QUEUE",
    "STAGE",
    "THOBE_RETURN",
    "LUNCH",
)

ACTIVITY_LABEL = {
    "REGISTRATION": "Registration",
    "THOBE_ALLOCATION": "Thobe Allocation",
    "SEATING": "Seating",
    "QUEUE": "Queue",
    "STAGE": "Stage",
    "THOBE_RETURN": "Thobe Return",
    "LUNCH": "Lunch",
}


def normalize_activity(value: str) -> str:
    """'thobe-allocation' / 'Thobe_Allocation' -> 'THOBE_ALLOCATION'; raises if unknown."""
    activity = (value or "").strip().upper().replace("-", "_")
    if activity not in ACTIVITY_LABEL:
        raise UnknownActivityError(activity)
    return activity


class UnknownActivityError(ValueError):
    code = "UNKNOWN_ACTIVITY"

    def __init__(self, activity: str):
        super().__init__(f"Unknown activity: {activity!r}")
        self.message = "That activity does not exist."

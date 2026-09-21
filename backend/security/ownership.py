"""Venue ownership: golden rule 4 / SYSTEM_SPEC 11.2 (single writer).

Every activity is recorded at exactly one venue. This module is the ONE place
the application asks "may this server originate a write for this activity?".
Later phases attach it to their write paths through `require_can_originate`
(backend/security/deps.py) instead of copying checks into each endpoint.

    College : REGISTRATION
    Stadium : THOBE_ALLOCATION, SEATING, QUEUE, STAGE
    Hall    : THOBE_RETURN, LUNCH
    Central : nothing. Central never originates a venue-owned write; venue events
              reach it only through sync (Phase 14).

The map mirrors the database function activity_owner() (migration 0002), which
enforces the same rule on every row. A test compares the two so they cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ACTIVITIES = (
    "REGISTRATION",
    "THOBE_ALLOCATION",
    "SEATING",
    "QUEUE",
    "STAGE",
    "THOBE_RETURN",
    "LUNCH",
)
VENUES = ("college", "stadium", "hall")

ACTIVITY_OWNER = {
    "REGISTRATION": "college",
    "THOBE_ALLOCATION": "stadium",
    "SEATING": "stadium",
    "QUEUE": "stadium",
    "STAGE": "stadium",
    "THOBE_RETURN": "hall",
    "LUNCH": "hall",
}

ACTIVITY_LABEL = {
    "REGISTRATION": "Registration",
    "THOBE_ALLOCATION": "Thobe Allocation",
    "SEATING": "Seating",
    "QUEUE": "Queue",
    "STAGE": "Stage",
    "THOBE_RETURN": "Thobe Return",
    "LUNCH": "Lunch",
}
VENUE_LABEL = {"college": "College", "stadium": "Stadium", "hall": "Hall"}


def normalize_activity(value: str) -> str:
    """'thobe-allocation' / 'Thobe_Allocation' -> 'THOBE_ALLOCATION'; raises if unknown."""
    activity = (value or "").strip().upper().replace("-", "_")
    if activity not in ACTIVITY_OWNER:
        raise UnknownActivityError(activity)
    return activity


class OwnershipError(Exception):
    """A write was attempted on a server that may not originate it. `message` is operator-safe."""

    code = "OWNERSHIP"

    def __init__(self, message: str, *, activity: str, owner: str):
        super().__init__(message)
        self.message = message
        self.activity = activity
        self.owner = owner


class WrongVenueError(OwnershipError):
    code = "WRONG_VENUE"


class CentralCannotOriginateError(OwnershipError):
    code = "CENTRAL_CANNOT_ORIGINATE"


class UnknownActivityError(ValueError):
    code = "UNKNOWN_ACTIVITY"

    def __init__(self, activity: str):
        super().__init__(f"Unknown activity: {activity!r}")
        self.message = "That activity does not exist."


@dataclass(frozen=True)
class CorrectionRoute:
    """Where an Admin correction to an activity is applied (SYSTEM_SPEC 16)."""

    owner_venue: str
    apply_here: bool  # True: this server owns the activity. False: send to owner_venue via sync.


@dataclass(frozen=True)
class VenueGuard:
    mode: str  # "venue" | "central"
    venue_id: Optional[str]

    def owns(self, activity: str) -> bool:
        return self.mode == "venue" and ACTIVITY_OWNER[normalize_activity(activity)] == self.venue_id

    def ensure_can_originate(self, activity: str) -> str:
        """Return the normalised activity, or raise. Call before ANY write of a venue-owned event."""
        activity = normalize_activity(activity)
        owner = ACTIVITY_OWNER[activity]
        label = ACTIVITY_LABEL[activity]
        if self.mode != "venue":
            raise CentralCannotOriginateError(
                f"{label} is recorded at the {VENUE_LABEL[owner]} server, never at the central server.",
                activity=activity,
                owner=owner,
            )
        if owner != self.venue_id:
            raise WrongVenueError(
                f"{label} is recorded at the {VENUE_LABEL[owner]} server, not here.",
                activity=activity,
                owner=owner,
            )
        return activity

    def route_correction(self, activity: str) -> CorrectionRoute:
        """Corrections are accepted everywhere; only the owning venue applies them itself."""
        activity = normalize_activity(activity)
        owner = ACTIVITY_OWNER[activity]
        return CorrectionRoute(owner_venue=owner, apply_here=(self.mode == "venue" and owner == self.venue_id))


def guard_from_settings(settings) -> VenueGuard:
    return VenueGuard(mode=settings.mode, venue_id=settings.venue_id)

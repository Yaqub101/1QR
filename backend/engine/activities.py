"""THE ONE FILE that configures the seven activities on the station engine.

Adding or changing an activity means editing ONLY this file (and its tests). Read
docs/STATION_CONTRACT.md first: it says what every key means, what is allowed, and what to do when
you need something these keys cannot express (stop and ask; do not touch the engine internals).

Sources: SYSTEM_SPEC section 2 (journey order), 3 (what each activity shows), 5 (status chain), 11.2
(single writer), 14 (messages); docs/TODO.md Phases 7-12.

Journey order (a prerequisite must come EARLIER in this list):
    REGISTRATION -> THOBE_ALLOCATION -> SEATING -> QUEUE -> STAGE -> THOBE_RETURN -> LUNCH
"""
from __future__ import annotations

from backend.engine.model import ActivityConfig, Prerequisite, validate_registry

ACTIVITY_CONFIGS: dict[str, ActivityConfig] = {
    # ---------------------------------------------------------------- College
    "REGISTRATION": ActivityConfig(
        activity="REGISTRATION",
        owning_venue="college",
        prerequisites=(),
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM REGISTRATION",
        duplicate_message="ALREADY REGISTERED — {time}",
        flag_rules=("late_registration",),  # after the cutoff: accepted and flagged LATE
    ),
    # ---------------------------------------------------------------- Stadium
    "THOBE_ALLOCATION": ActivityConfig(
        activity="THOBE_ALLOCATION",
        owning_venue="stadium",
        prerequisites=(
            # Registration happens at the College: a CROSS-venue prerequisite (freshness rule: cross_venue.py).
            Prerequisite("REGISTRATION", "THOBE NOT AVAILABLE — REGISTRATION PENDING"),
        ),
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM THOBE GIVEN",
        duplicate_message="THOBE ALREADY ALLOCATED — {time}",
    ),
    "SEATING": ActivityConfig(
        activity="SEATING",
        owning_venue="stadium",
        prerequisites=(Prerequisite("THOBE_ALLOCATION", "SEATING NOT AVAILABLE — THOBE NOT RECEIVED"),),
        # The university's data has no Seat Number column, so there is no seat to show and none to
        # record: Seating is a plain "this student is seated" checkpoint, like Thobe Allocation.
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM SEATED",
        duplicate_message="SEATING ALREADY CONFIRMED — {time}",
    ),
    "QUEUE": ActivityConfig(
        activity="QUEUE",
        owning_venue="stadium",
        prerequisites=(Prerequisite("SEATING", "QUEUE NOT AVAILABLE — SEATING PENDING"),),
        # Order on stage is the order these confirmations happen in — first come, first shown.
        display_fields=("prn", "queue_position"),
        confirm_label="CONFIRM QUEUE",
        duplicate_message="ALREADY IN QUEUE — POSITION {queue_position} — {time}",
        effects=("enqueue",),  # takes the next queue position in the same transaction
    ),
    "STAGE": ActivityConfig(
        activity="STAGE",
        owning_venue="stadium",
        prerequisites=(Prerequisite("QUEUE", "STAGE NOT AVAILABLE — QUEUE PENDING"),),
        display_fields=("programme", "school"),
        confirm_label="COMPLETE",
        duplicate_message="DEGREE ALREADY RECEIVED — {time}",
    ),
    # ---------------------------------------------------------------- Hall
    "THOBE_RETURN": ActivityConfig(
        activity="THOBE_RETURN",
        owning_venue="hall",
        prerequisites=(
            # Both are recorded at the Stadium: CROSS-venue prerequisites (freshness rule: cross_venue.py).
            Prerequisite("STAGE", "THOBE RETURN NOT AVAILABLE — STAGE PENDING"),
            Prerequisite("THOBE_ALLOCATION", "THOBE RETURN NOT AVAILABLE — NO THOBE WAS ISSUED"),
        ),
        display_fields=("prn", "thobe_issued"),
        confirm_label="CONFIRM RETURN",
        duplicate_message="ALREADY RETURNED — {time}",
    ),
    "LUNCH": ActivityConfig(
        activity="LUNCH",
        owning_venue="hall",
        # Same venue as Thobe Return: a hard block. An Admin "Return Waived / Lost" counts as the return.
        prerequisites=(Prerequisite("THOBE_RETURN", "LUNCH NOT AVAILABLE — THOBE RETURN PENDING"),),
        display_fields=("prn", "eligibility"),
        confirm_label="CONFIRM LUNCH",
        duplicate_message="LUNCH ALREADY CLAIMED — {time}",
    ),
}

validate_registry(ACTIVITY_CONFIGS)  # a wrong entry stops the server at startup, not on event day

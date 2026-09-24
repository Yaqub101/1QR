"""THE ONE FILE that configures the seven activities on the station engine.

Adding or changing an activity means editing ONLY this file (and its tests). Read
docs/STATION_CONTRACT.md first: it says what every key means, what is allowed, and what to do when
you need something these keys cannot express (stop and ask; do not touch the engine internals).

Sources: SYSTEM_SPEC section 2 (journey order), 3 (what each activity shows), 5 (status chain),
14 (messages); docs/TODO.md Phases 7-12.

Journey order (a prerequisite must come EARLIER in this list):
    REGISTRATION -> THOBE_ALLOCATION -> MONEY_RECEIVED -> SEATING -> QUEUE -> STAGE -> THOBE_RETURN
        -> MONEY_RETURNED -> LUNCH

Since the role/flow redesign, REGISTRATION, the robe and the money (entry) and their returns are recorded at
the ONE Registry desk with tick boxes (backend/engine/registry.py), and SEATING is optional: nothing requires it.
"""
from __future__ import annotations

from backend.engine.model import ActivityConfig, Prerequisite, validate_registry

ACTIVITY_CONFIGS: dict[str, ActivityConfig] = {
    "REGISTRATION": ActivityConfig(
        activity="REGISTRATION",
        prerequisites=(),
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM REPORTING",
        duplicate_message="ALREADY REPORTED — {time}",
        flag_rules=("late_registration",),  # after the cutoff: accepted and flagged LATE
    ),
    "THOBE_ALLOCATION": ActivityConfig(
        activity="THOBE_ALLOCATION",
        prerequisites=(Prerequisite("REGISTRATION", "ROBE NOT AVAILABLE — REPORTING PENDING"),),
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM ROBE GIVEN",
        duplicate_message="ROBE ALREADY ALLOCATED — {time}",
    ),
    "SEATING": ActivityConfig(
        activity="SEATING",
        prerequisites=(Prerequisite("THOBE_ALLOCATION", "SEATING NOT AVAILABLE — ROBE NOT RECEIVED"),),
        # The university's data has no Seat Number column, so there is no seat to show and none to
        # record: Seating is a plain "this student is seated" checkpoint, like Robe Allocation.
        display_fields=("prn", "programme", "school"),
        confirm_label="CONFIRM SEATED",
        duplicate_message="SEATING ALREADY CONFIRMED — {time}",
    ),
    "QUEUE": ActivityConfig(
        activity="QUEUE",
        # Seating is optional since the role/flow redesign (a checkpoint that never blocks anything), so
        # the Queue needs the robe, not a seat.
        prerequisites=(Prerequisite("THOBE_ALLOCATION", "QUEUE NOT AVAILABLE — ROBE NOT RECEIVED"),),
        # Order on stage is the order these confirmations happen in — first come, first shown.
        display_fields=("prn", "queue_position"),
        confirm_label="CONFIRM QUEUE",
        duplicate_message="ALREADY IN QUEUE — POSITION {queue_position} — {time}",
        effects=("enqueue",),  # takes the next queue position in the same transaction
    ),
    "STAGE": ActivityConfig(
        activity="STAGE",
        prerequisites=(Prerequisite("QUEUE", "STAGE NOT AVAILABLE — QUEUE PENDING"),),
        display_fields=("programme", "school"),
        # The Stage operator's NEXT records this (redesign R2: advancing IS the "degree received" record).
        confirm_label="NEXT",
        duplicate_message="DEGREE ALREADY RECEIVED — {time}",
    ),
    "THOBE_RETURN": ActivityConfig(
        activity="THOBE_RETURN",
        prerequisites=(
            Prerequisite("STAGE", "ROBE RETURN NOT AVAILABLE — STAGE PENDING"),
            Prerequisite("THOBE_ALLOCATION", "ROBE RETURN NOT AVAILABLE — NO ROBE WAS ISSUED"),
        ),
        display_fields=("prn", "thobe_issued"),
        confirm_label="CONFIRM RETURN",
        duplicate_message="ALREADY RETURNED — {time}",
    ),
    "LUNCH": ActivityConfig(
        activity="LUNCH",
        # An Admin "Return Waived / Lost" counts as the return.
        prerequisites=(Prerequisite("THOBE_RETURN", "LUNCH NOT AVAILABLE — ROBE RETURN PENDING"),),
        display_fields=("prn", "eligibility"),
        confirm_label="CONFIRM LUNCH",
        duplicate_message="LUNCH ALREADY CLAIMED — {time}",
    ),
}

validate_registry(ACTIVITY_CONFIGS)  # a wrong entry stops the server at startup, not on event day

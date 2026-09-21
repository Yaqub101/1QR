"""Cross-venue prerequisites: THE PHASE 15 HOOK. (SYSTEM_SPEC 11.5, TODO Phase 15)

A prerequisite is 'cross-venue' when the activity that must come first is recorded at a DIFFERENT
venue (Thobe Allocation needs College's Registration; Thobe Return needs the Stadium's Stage and
Thobe Allocation). Same-venue prerequisites never come here: they are hard blocks.

    >>> TODO(Phase 15): THIS IS A STUB. It ALWAYS ALLOWS, provisional or not. <<<

Phase 15 replaces the BODY of `check_cross_venue_prerequisite` with the fresh/stale rule:

    present locally                              -> allow
    missing, owning venue synced recently        -> block  ("NOT AVAILABLE - ... PENDING")
      (within settings.freshness_window_seconds, sync_state.last_success_at)
    missing, owning venue's data is stale        -> allow as PROVISIONAL (operator sees a normal
                                                    confirmation; an OPEN exception is raised later)

The engine already honours every field of CrossVenueDecision (block + message, provisional flag,
PROVISIONAL scan_log result), and tests/test_station_engine.py::TestCrossVenueHook proves it. Nothing
else in the engine changes in Phase 15. The test named ..._PHASE_15_MUST_REPLACE_THIS_... asserts
the stub behaviour on purpose: it will fail the day the real rule lands, so it cannot be forgotten.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.engine import Connection

from backend.engine.model import Prerequisite

# Flip to True in Phase 15, together with replacing the stub test.
CROSS_VENUE_RULES_IMPLEMENTED = False


@dataclass(frozen=True)
class CrossVenueDecision:
    allow: bool
    provisional: bool = False       # allow=True and provisional=True -> event flagged PROVISIONAL
    message: Optional[str] = None   # shown when allow=False (falls back to the prerequisite's own message)


def check_cross_venue_prerequisite(
    conn: Connection,
    *,
    student_id,
    prerequisite: Prerequisite,
    this_venue: str,
    present_locally: bool,
) -> CrossVenueDecision:
    # TODO(Phase 15): implement the fresh/stale rule described in the module docstring.
    # Until then every cross-venue prerequisite is treated as satisfied.
    return CrossVenueDecision(allow=True, provisional=False)

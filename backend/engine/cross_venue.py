"""Cross-venue prerequisites (SYSTEM_SPEC 11.5, TODO Phase 15).

A prerequisite is "cross-venue" when the activity that must come first is recorded at a DIFFERENT venue
(Thobe Allocation needs College's Registration; Thobe Return needs the Stadium's Stage and Thobe
Allocation). Same-venue prerequisites never come here: the pipeline hard-blocks them itself, because that
data is local and current (golden rule 8). The engine calls this ONE hook for every cross-venue prerequisite.

    present in this server's database        -> ALLOW
    missing, owning venue's data is FRESH    -> BLOCK    the student genuinely has not done it
    missing, owning venue's data is STALE    -> ALLOW as PROVISIONAL (the operator sees a normal
                                                confirmation; an OPEN exception is raised for the Admin
                                                and closes itself when the record arrives via sync)

"Fresh" is `sync_state.data_as_of` for the owning venue being within the freshness window (default 2
minutes; `settings.freshness_window_seconds`). That timestamp is maintained by the pull worker
(backend/sync/status.py). A venue that has never synced counts as stale: a network hiccup, or a server
that is not connected to central at all, must never stop the event ("internet failure is not event failure").

"Present" means an active completion of that activity is stored HERE, whichever venue wrote it: events
from other venues are replicated in as read-only history, so they satisfy a prerequisite like any other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.engine import Connection

from backend.engine.model import Prerequisite
from backend.security import ownership
from backend.sync import status


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
    if present_locally:
        return CrossVenueDecision(allow=True)
    owner = ownership.ACTIVITY_OWNER[prerequisite.activity]
    if status.peer_is_fresh(conn, owner):
        return CrossVenueDecision(allow=False, message=prerequisite.missing_message)
    return CrossVenueDecision(allow=True, provisional=True)

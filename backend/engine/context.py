"""Per-request context shared by the engine's parts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.engine.model import ActivityConfig


@dataclass(frozen=True)
class EngineContext:
    settings: Any        # backend.config.Settings
    principal: Any       # backend.security.sessions.Principal
    station: dict        # {"station_id", "venue_id", "activity"} - the station decides the activity
    config: ActivityConfig

    @property
    def activity(self) -> str:
        return self.station["activity"]

    @property
    def venue(self) -> str:
        return self.settings.venue_id

"""Per-request context shared by the engine's parts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.engine.model import ActivityConfig


@dataclass(frozen=True)
class EngineContext:
    settings: Any        # backend.config.Settings
    principal: Any       # backend.security.sessions.Principal
    activity: str         # which of the seven activities this request is for
    config: ActivityConfig

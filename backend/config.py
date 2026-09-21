import os
from typing import Optional
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_MODES = ("venue", "central")
VALID_VENUES = ("college", "stadium", "hall")


class Settings(BaseSettings):
    mode: str
    venue_id: Optional[str] = None
    database_url: str = "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db"
    central_url: Optional[str] = None
    venue_api_key: Optional[str] = None
    late_cutoff: Optional[str] = None
    event_name: str = "Convocation Ceremony"
    log_dir: str = "logs"
    log_level: str = "INFO"

    # Sync (Phase 14). A venue pushes its outbox to central and pulls the other venues' events. The API key is
    # issued at central (`python -m backend.sync.keys issue --venue <v>`) and is stored there only as a SHA-256.
    sync_enabled: bool = True
    sync_interval_seconds: float = 5.0        # between cycles while healthy
    sync_batch_size: int = 200                # events per request
    sync_timeout_seconds: float = 10.0
    sync_backoff_base_seconds: float = 2.0    # first retry delay after a failed cycle; doubles each failure
    sync_backoff_max_seconds: float = 60.0
    sync_max_attempts: int = 20               # times a transient refusal (student not on central yet) is retried
    sync_require_tls: bool = True             # refuse to send the API key over plain http (set false only in dev/tests)
    central_ca_file: Optional[str] = None     # a private CA / self-signed certificate for central, if any

    # Backups (Phase 17). The directory should be on a SECOND device.
    backup_dir: Optional[str] = None
    backup_interval_seconds: int = 300        # SYSTEM_SPEC 21: a full dump every 5 minutes
    backup_keep: int = 24                     # newest automatic dumps kept (milestones are kept forever)

    # Clock used for the times operators read on screen ("11:21 AM"). A fixed offset in minutes from
    # UTC, so it works offline and on any OS with no timezone database. 330 = India (no daylight saving).
    event_utc_offset_minutes: int = 330

    # Sessions (Phase 5). Sized for a multi-hour event day: an operator who keeps
    # scanning is never logged out; a laptop left alone for 2 hours is; nothing
    # lives past 12 hours, so the next morning starts with a fresh login.
    session_idle_minutes: int = 120
    session_max_hours: int = 12
    # Set true only where the site is served over HTTPS (central). Venue LANs are plain HTTP.
    cookie_secure: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("mode")
    @classmethod
    def validate_mode_field(cls, v: str) -> str:
        v_lower = v.lower() if isinstance(v, str) else v
        if v_lower not in VALID_MODES:
            raise ValueError(
                f"Invalid MODE '{v}'. MODE must be one of: {', '.join(VALID_MODES)}."
            )
        return v_lower

    @model_validator(mode="after")
    def validate_venue_for_mode(self) -> "Settings":
        if self.mode == "venue":
            if not self.venue_id:
                raise ValueError(
                    f"VENUE_ID is required when MODE is 'venue'. Allowed values: {', '.join(VALID_VENUES)}."
                )
            venue_id_lower = self.venue_id.lower()
            if venue_id_lower not in VALID_VENUES:
                raise ValueError(
                    f"Unknown VENUE_ID '{self.venue_id}'. Allowed values for venue mode: {', '.join(VALID_VENUES)}."
                )
            self.venue_id = venue_id_lower
        elif self.mode == "central":
            self.venue_id = None

        return self


def get_settings() -> Settings:
    return Settings()

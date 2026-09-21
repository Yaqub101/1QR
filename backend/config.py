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

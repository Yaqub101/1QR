from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://convocation_user:convocation_password@localhost:5432/convocation_db"
    late_cutoff: Optional[str] = None
    event_name: str = "Convocation Ceremony"
    log_dir: str = "logs"
    log_level: str = "INFO"
    # Where an uploaded student list waits while the Admin works through the import screen
    # (backend/import_staging.py). Unset means the system temp folder; point it at the same
    # encrypted volume as the rest of the student data.
    import_staging_dir: Optional[str] = None

    # Where student photo files live (backend/photo_storage.py). "local" = a folder (photos/ at the
    # project root unless PHOTO_STORAGE_DIR says otherwise); "cloudinary" = private Cloudinary images,
    # for Render, whose own disk is wiped on every deploy. A misconfiguration stops the server at start.
    photo_storage: str = "local"
    photo_storage_dir: Optional[str] = None
    cloudinary_cloud_name: Optional[str] = None
    cloudinary_api_key: Optional[str] = None
    cloudinary_api_secret: Optional[str] = None
    cloudinary_folder: str = "convocation/student-photos"

    # Backups (Phase 17). The directory should be on a SECOND device.
    backup_dir: Optional[str] = None
    backup_interval_seconds: int = 300        # SYSTEM_SPEC 21: a full dump every 5 minutes
    backup_keep: int = 24                     # newest automatic dumps kept (milestones are kept forever)

    # Clock used for the times operators read on screen ("11:21 AM"). A fixed offset in minutes from
    # UTC, so it works offline and on any OS with no timezone database. 330 = India (no daylight saving).
    event_utc_offset_minutes: int = 330

    # Sessions (Phase 5). Sized for a multi-hour event day: an operator who keeps
    # scanning is never logged out; a browser left alone for 2 hours is; nothing
    # lives past 12 hours, so the next morning starts with a fresh login.
    session_idle_minutes: int = 120
    session_max_hours: int = 12
    # Set true only where the site is served over HTTPS.
    cookie_secure: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()

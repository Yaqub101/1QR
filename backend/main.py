from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import FastAPI

from backend.config import Settings, get_settings
from backend.logging_config import setup_logging
from backend import database


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)

    engine = database.get_engine(settings.database_url)

    app = FastAPI(
        title=f"Convocation System ({settings.mode.capitalize()} Mode)",
        version="0.1.0",
    )

    app.state.settings = settings
    app.state.engine = engine

    @app.get("/health")
    def health_check() -> Dict[str, Any]:
        is_db_up = database.check_db_health(engine=engine)
        return {
            "mode": settings.mode,
            "venue": settings.venue_id,
            "db": "up" if is_db_up else "down",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return app


def get_app() -> FastAPI:
    return create_app()


try:
    app = get_app()
except Exception:
    app = None

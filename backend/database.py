import logging
from typing import Dict, List, Optional, Tuple
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

Base = declarative_base()

# The tables this server cannot serve a single scan without. They are created by the migrations
# (`alembic upgrade head`), never by SQLAlchemy metadata, so their presence is the only honest
# answer to "has this database been set up?".
REQUIRED_TABLES = (
    "alembic_version",
    "students",
    "display_snapshot",
    "qr_tokens",
    "activity_events",
    "scan_log",
    "users",
    "sessions",
    "queue",
    "counters",
    "exceptions",
    "audit_log",
    "settings",
    "programme_faculty",
)

# What /health puts in "db".
UP = "up"                # reachable, and the schema is there
NOT_READY = "not_ready"  # reachable, but the migrations have not been run (or not fully)
DOWN = "down"            # not reachable at all

_engines: Dict[str, Engine] = {}
_sessionmakers: Dict[str, sessionmaker] = {}


def get_engine(database_url: str) -> Engine:
    global _engines
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url not in _engines:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        _engines[database_url] = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _engines[database_url]


def get_sessionmaker(database_url: str) -> sessionmaker:
    global _sessionmakers
    if database_url not in _sessionmakers:
        engine = get_engine(database_url)
        _sessionmakers[database_url] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _sessionmakers[database_url]


def _resolve(engine: Optional[Engine], database_url: Optional[str]) -> Optional[Engine]:
    if engine is None and database_url is not None:
        return get_engine(database_url)
    return engine


def check_db_health(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> bool:
    """Is the database REACHABLE? Says nothing about whether it holds a schema: an empty database
    answers `SELECT 1` perfectly happily. Use db_status() to decide whether this server can work."""
    try:
        engine = _resolve(engine, database_url)
        if engine is None:
            return False

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("Database health check failed: %s", e)
        return False


def missing_tables(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> List[str]:
    """Which of REQUIRED_TABLES are not there, in the documented order. Raises if the database
    cannot be reached (the caller tells "unreachable" from "not set up")."""
    engine = _resolve(engine, database_url)
    if engine is None:
        raise ValueError("missing_tables needs an engine or a database_url")
    present = set(inspect(engine).get_table_names())
    return [t for t in REQUIRED_TABLES if t not in present]


def db_status(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> Tuple[str, List[str]]:
    """(status, missing tables). The schema is part of being "up": a server whose migrations have
    never run is reachable and completely unable to work, and must not report itself healthy."""
    try:
        engine = _resolve(engine, database_url)
        if engine is None:
            return DOWN, []
        missing = missing_tables(engine=engine)
    except Exception as e:
        logger.warning("Database health check failed: %s", e)
        return DOWN, []
    if missing:
        logger.warning("database is reachable but not set up; missing tables: %s. Run `alembic upgrade head`.",
                       ", ".join(missing))
        return NOT_READY, missing
    return UP, []


import functools
import pathlib
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_INI_PATH = pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"


@functools.lru_cache(maxsize=1)
def get_expected_schema_version() -> str:
    """Return the revision ID of the latest migration script (Alembic head)."""
    cfg = Config(str(ALEMBIC_INI_PATH))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    return head or ""


def get_db_schema_version(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> Optional[str]:
    """Read the current revision ID from the alembic_version table in the database."""
    engine = _resolve(engine, database_url)
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            table_names = inspect(conn).get_table_names()
            if "alembic_version" not in table_names:
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            return str(row) if row is not None else None
    except Exception as e:
        logger.warning("Failed to query alembic_version: %s", e)
        return None


def check_schema_version(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> Tuple[bool, Optional[str], str]:
    """Compare DB alembic_version with code Alembic head. Returns (is_match, db_version, expected_head)."""
    expected = get_expected_schema_version()
    db_ver = get_db_schema_version(engine=engine, database_url=database_url)
    return (db_ver == expected, db_ver, expected)


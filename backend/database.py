import logging
from typing import Dict, Optional
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

Base = declarative_base()

_engines: Dict[str, Engine] = {}
_sessionmakers: Dict[str, sessionmaker] = {}


def get_engine(database_url: str) -> Engine:
    global _engines
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


def check_db_health(engine: Optional[Engine] = None, database_url: Optional[str] = None) -> bool:
    try:
        if engine is None and database_url is not None:
            engine = get_engine(database_url)
        if engine is None:
            return False

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.warning("Database health check failed: %s", e)
        return False

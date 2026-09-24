"""Engine and session management (synchronous SQLAlchemy 2 with psycopg 3)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from nerva.core.config import get_settings


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: services commit, then the route serializes the returned
    # objects without triggering extra refresh queries.
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back if not committed."""
    with make_session_factory(get_engine())() as session:
        yield session

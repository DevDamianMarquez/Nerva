"""Small helpers shared by the test modules."""

import re
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from nerva.core.security import SESSION_COOKIE_NAME


@dataclass(frozen=True)
class Account:
    """A user signed up and logged in through the API, plus the cookie that
    authenticates as them (each account is logged in on its own session, so tests can
    hold several authenticated accounts at once without one overwriting another's)."""

    id: str
    email: str
    session_token: str

    @property
    def cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE_NAME: self.session_token}


def ensure_database(url: str) -> None:
    """Create the database named in `url` if it does not exist yet."""
    parsed = make_url(url)
    name = parsed.database or ""
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError(f"Unsafe database name: {name!r}")
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()

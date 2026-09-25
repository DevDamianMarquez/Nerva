"""The Alembic migrations must produce exactly the schema the models describe."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

import nerva.models  # noqa: F401
from nerva.db.base import Base
from tests.helpers import ensure_database

API_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "actions",
    "activity_events",
    "companies",
    "contacts",
    "conversations",
    "integrations",
    "memories",
    "messages",
    "oauth_states",
    "opportunities",
    "sessions",
    "users",
    "workspace_members",
    "workspaces",
}


@pytest.fixture
def scratch_engine(test_database_url: str) -> Iterator[Engine]:
    """An empty database of its own, so this test cannot disturb the shared test schema."""
    url = make_url(test_database_url).set(database="nerva_migrations_test")
    ensure_database(url.render_as_string(hide_password=False))
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_config(scratch_engine: Engine) -> Config:
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    url = scratch_engine.url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def test_upgrading_to_head_creates_the_schema_the_models_describe(
    alembic_config: Config, scratch_engine: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    with scratch_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == EXPECTED_TABLES
        differences = compare_metadata(
            MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata
        )

    assert differences == [], "models and migrations have drifted; run `npm run api:revision`"


def test_downgrading_to_base_removes_everything(
    alembic_config: Config, scratch_engine: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, "base")

    assert set(inspect(scratch_engine).get_table_names()) == {"alembic_version"}

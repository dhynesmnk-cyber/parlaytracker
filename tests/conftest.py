"""Suite-wide rules and the shared database: DB tests need TEST_DATABASE_URL, live tests run
only when selected, and the test database is built by the Alembic migrations."""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from parlaytracker.core.config import normalize_database_url


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_db = bool(os.environ.get("TEST_DATABASE_URL"))
    if not has_db and os.environ.get("CI"):
        raise pytest.UsageError("TEST_DATABASE_URL must be set in CI")
    run_live = "live" in (config.getoption("markexpr") or "")
    for item in items:
        if "db" in item.keywords and not has_db:
            item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL not set (see HANDOVER.md)"))
        if "live" in item.keywords and not run_live:
            item.add_marker(pytest.mark.skip(reason="live test: run with -m live"))


ROOT = Path(__file__).resolve().parents[1]


def alembic_config(connection: Connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    return cfg


@pytest.fixture
def make_alembic_config():
    return alembic_config


@pytest.fixture(scope="session")
def engine() -> Engine:
    url = normalize_database_url(os.environ["TEST_DATABASE_URL"])
    database = make_url(url).database or ""
    if "test" not in database:
        pytest.exit(f"refusing to wipe database {database!r}: its name must contain 'test'", 2)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        command.upgrade(alembic_config(conn), "head")
    yield eng
    eng.dispose()

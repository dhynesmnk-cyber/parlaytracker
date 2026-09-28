"""Fixtures for tests against a real PostgreSQL built by the Alembic migrations."""
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from parlaytracker.core.config import normalize_database_url
from parlaytracker.core.models import Event, Sport, Sportsbook, Tag

ROOT = Path(__file__).resolve().parents[2]


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


@pytest.fixture
def session(engine: Engine) -> Session:
    """Everything a test does is rolled back afterwards; commits become savepoints."""
    with engine.connect() as conn:
        outer = conn.begin()
        s = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        try:
            yield s
        finally:
            s.close()
            outer.rollback()


@pytest.fixture
def book(session: Session) -> Sportsbook:
    return session.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()


def _event(session: Session, sport: Sport, espn_id: str) -> Event:
    event = Event(
        sport=sport,
        espn_event_id=espn_id,
        home_team="Home Team",
        away_team="Away Team",
        home_espn_team_id="1",
        away_espn_team_id="2",
        start_time=datetime(2026, 10, 4, 17, 0, tzinfo=UTC),
    )
    session.add(event)
    session.flush()
    return event


@pytest.fixture
def nfl_event(session: Session) -> Event:
    return _event(session, Sport.NFL, "401001")


@pytest.fixture
def nfl_event_2(session: Session) -> Event:
    return _event(session, Sport.NFL, "401002")


@pytest.fixture
def nba_event(session: Session) -> Event:
    return _event(session, Sport.NBA, "401003")


@pytest.fixture
def tag(session: Session) -> Tag:
    t = Tag(category="situation", name="primetime")
    session.add(t)
    session.flush()
    return t

"""Fixtures for tests against a real PostgreSQL built by the Alembic migrations."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from parlaytracker.core.models import Event, Sport, Sportsbook, Tag


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


@pytest.fixture
def clean(engine: Engine):
    """For tests whose code commits for real (the worker's jobs): empty the tables after."""
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE slips, legs, leg_tags, events, source_health, raw_samples "
                          "RESTART IDENTITY CASCADE"))

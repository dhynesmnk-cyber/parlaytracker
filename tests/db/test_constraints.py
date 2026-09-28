"""Database-level rejections from SPEC.md section 11 ("Rejected by the database").

These insert through the ORM or raw SQL, deliberately bypassing SlipIn, to prove the
database itself refuses bad rows.
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from parlaytracker.core.models import (
    DataSource,
    EntrySource,
    Event,
    FailureKind,
    HealthState,
    Leg,
    LegResult,
    MarketType,
    RawSample,
    Slip,
    SlipType,
    SourceHealth,
    Sportsbook,
    Tag,
    TeamSide,
    leg_tags,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)


def rejects(session: Session, obj=None, sql: str | None = None) -> None:
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            if sql:
                session.execute(text(sql))
            else:
                session.add(obj)
                session.flush()


@pytest.fixture
def slip(session: Session, book: Sportsbook) -> Slip:
    s = Slip(logged_by="a@example.com", is_placed=True, slip_type=SlipType.PARLAY,
             sportsbook_id=book.id, american_odds=264, stake=Decimal("10.00"),
             source=EntrySource.QUICK_ADD)
    session.add(s)
    session.flush()
    return s


@pytest.fixture
def leg(slip: Slip, nfl_event: Event):
    """Build (not add) a valid game-total leg with overrides."""
    def make(**overrides) -> Leg:
        values = {"slip_id": slip.id, "event_id": nfl_event.id,
                  "market_type": MarketType.GAME_TOTAL, "line": Decimal("45.5"),
                  "american_odds": -110}
        return Leg(**{**values, **overrides})
    return make


def test_valid_rows_are_accepted(session, leg, tag):
    session.add_all([
        leg(tags=[tag]),
        leg(market_type=MarketType.ALT_SPREAD, side=TeamSide.HOME, line=Decimal("-7.5")),
        leg(market_type=MarketType.PLAYER_RECEPTIONS, espn_athlete_id="123", line=Decimal("4.5")),
        leg(market_type=MarketType.OTHER, line=None, description="Chiefs ML"),
        leg(result=LegResult.WIN, final_value=Decimal("47"), settled_at=NOW,
            settlement_source=DataSource.ESPN_WEB, verified_at=NOW,
            verified_source=DataSource.NFLVERSE, live_source=DataSource.ESPN_SITE),
        SourceHealth(source="espn_web", state=HealthState.OPEN,
                     failure_kind=FailureKind.BLOCKED, open_until=NOW),
        RawSample(source="espn_site", reason="failure", url="https://x", status_code=403,
                  body="<html>Access Denied</html>"),
    ])
    session.flush()
    assert session.scalar(select(func.count()).select_from(Leg)) == 5


@pytest.mark.parametrize("odds", [50, -99, 99])
def test_leg_odds_between_minus_99_and_99(session, leg, odds):
    rejects(session, leg(american_odds=odds))


def test_slip_odds_between_minus_99_and_99(session, book):
    rejects(session, Slip(logged_by="a", is_placed=False, slip_type=SlipType.SINGLE,
                          sportsbook_id=book.id, american_odds=-99,
                          source=EntrySource.QUICK_ADD))


def test_team_market_without_side(session, leg):
    rejects(session, leg(market_type=MarketType.TEAM_TOTAL))


def test_side_on_non_team_market(session, leg):
    rejects(session, leg(side=TeamSide.AWAY))


def test_player_market_without_athlete(session, leg):
    rejects(session, leg(market_type=MarketType.PLAYER_POINTS))


def test_non_other_leg_without_line(session, leg):
    rejects(session, leg(line=None))


def test_other_leg_without_description(session, leg):
    rejects(session, leg(market_type=MarketType.OTHER, line=None))


def test_placed_slip_without_stake(session, book):
    rejects(session, Slip(logged_by="a", is_placed=True, slip_type=SlipType.SINGLE,
                          sportsbook_id=book.id, american_odds=-110,
                          source=EntrySource.QUICK_ADD))


def test_duplicate_tag(session, tag):
    rejects(session, Tag(category=tag.category, name=tag.name))


def test_settled_leg_without_settlement_source(session, leg):
    rejects(session, leg(result=LegResult.WIN))


def test_pending_leg_with_settlement_source(session, leg):
    rejects(session, leg(settlement_source=DataSource.ESPN_WEB))


def test_verified_at_without_source(session, leg):
    rejects(session, leg(verified_at=NOW))


def test_verified_source_without_time(session, leg):
    rejects(session, leg(verified_source=DataSource.NFLVERSE))


def test_unknown_market_value(session, slip, nfl_event):
    rejects(session, sql=(
        "INSERT INTO legs (slip_id, event_id, market_type, line, result, needs_review) "
        f"VALUES ({slip.id}, {nfl_event.id}, 'moneyline', 1, 'pending', false)"))


def test_unknown_data_source(session, slip, nfl_event):
    rejects(session, sql=(
        "INSERT INTO legs (slip_id, event_id, market_type, line, result, needs_review, "
        f"live_source) VALUES ({slip.id}, {nfl_event.id}, 'game_total', 1, 'pending', false, "
        "'yahoo')"))


def test_unknown_event_status(session):
    rejects(session, sql=(
        "INSERT INTO events (sport, espn_event_id, home_team, away_team, home_espn_team_id, "
        "away_espn_team_id, start_time, status) "
        "VALUES ('nfl', '9', 'A', 'B', '1', '2', now(), 'halftime')"))


def test_unknown_failure_kind(session):
    rejects(session, sql=(
        "INSERT INTO source_health (source, state, failure_kind, consecutive_failures, "
        "requests_last_hour, errors_last_hour) VALUES ('x', 'open', 'flaky', 0, 0, 0)"))


def test_raw_sample_bad_reason(session):
    rejects(session, RawSample(source="espn_web", reason="debug", url="u", body="b"))


def test_deleting_slip_cascades_to_legs_and_leg_tags_but_keeps_tags(session, slip, leg, tag):
    session.add(leg(tags=[tag]))
    session.flush()
    session.delete(slip)
    session.flush()
    assert session.scalar(select(func.count()).select_from(Leg)) == 0
    assert session.scalar(select(func.count()).select_from(leg_tags)) == 0
    assert session.get(Tag, tag.id) is not None

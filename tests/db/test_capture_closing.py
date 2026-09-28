"""`capture_closing` end to end against Postgres, with the Odds API answered from the recorded
fixtures (SPEC.md section 8.2). No test spends credits."""
import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from parlaytracker.core import services as svc
from parlaytracker.core.models import (
    ClosingSource,
    Event,
    FailureKind,
    Leg,
    LegResult,
    RawSample,
    Sport,
    Sportsbook,
)
from parlaytracker.core.schemas import SlipIn
from parlaytracker.ingest.http import FetchError
from parlaytracker.ingest.odds_api import ApiEvent, EventOdds, RequestRejected
from parlaytracker.ingest.router import Breakers, ProviderOpen
from parlaytracker.worker.jobs import ClosingCapture

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds_api"
KICKOFF = datetime(2026, 9, 29, 0, 15, tzinfo=UTC)  # PHI @ CHI
NOW = KICKOFF - timedelta(minutes=3)
GAME = "47dc7baa254659f3beb2ed2b38c207b6"


class FakeOdds:
    """Answers from the fixtures, like the real API: only the requested markets, and one
    credit per market."""

    def __init__(self, quota: int | None = 400):
        self.quota_remaining = quota
        self.calls: list[tuple] = []
        self.fail_next: list[Exception] = []
        self._events = TypeAdapter(list[ApiEvent]).validate_python(
            json.loads((FIXTURES / "nfl_events_2026-09-28.json").read_text()))
        self._odds = json.loads(
            (FIXTURES / "nfl_event_odds_2026-09-28_PHI-CHI.json").read_text())

    def events(self, sport):
        self.calls.append(("events", sport))
        self._maybe_fail()
        return self._events

    def event_odds(self, sport, event_id, markets):
        self.calls.append(("odds", event_id, list(markets)))
        self._maybe_fail()
        cost = len(markets)
        if self.quota_remaining is not None:
            self.quota_remaining -= cost
        data = copy.deepcopy(self._odds)
        for book in data["bookmakers"]:
            book["markets"] = [m for m in book["markets"] if m["key"] in markets]
        return EventOdds.model_validate(data)

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    @property
    def odds_calls(self):
        return [c for c in self.calls if c[0] == "odds"]


@pytest.fixture
def clean(engine: Engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE slips, legs, leg_tags, events, source_health, raw_samples "
                          "RESTART IDENTITY CASCADE"))


def book_id(session: Session, name: str) -> int:
    return session.scalars(select(Sportsbook).where(Sportsbook.name == name)).one().id


def add_event(session: Session, start=KICKOFF, home="Chicago Bears",
              away="Philadelphia Eagles", espn_id="401900001") -> Event:
    event = Event(sport=Sport.NFL, espn_event_id=espn_id, home_team=home, away_team=away,
                  home_espn_team_id="3", away_espn_team_id="21", start_time=start)
    session.add(event)
    session.flush()
    return event


def add_slip(session: Session, event: Event, legs: list[dict], book="DraftKings"):
    single = len(legs) == 1  # several legs on one game are a same-game parlay
    data = {"is_placed": True, "stake": "10.00", "slip_type": "single" if single else "sgp",
            "sportsbook_id": book_id(session, book), "american_odds": -110,
            "source": "quick_add",
            "legs": [{"event_id": event.id, "american_odds": -110 if single else None, **leg}
                     for leg in legs]}
    return svc.create_slip(session, SlipIn(**data), "a@example.com")


def total(line="42.5"):
    return {"market_type": "game_total", "line": line}


def receptions(line="5.5"):
    return {"market_type": "player_receptions", "espn_athlete_id": "4241478",
            "player_name": "DeVonta Smith", "line": line}


@pytest.fixture
def job(engine, clean):
    odds = FakeOdds()
    breakers = Breakers(engine=None)
    return ClosingCapture(engine, odds, breakers, reserve=50), odds


def commit(engine, build):
    with Session(engine, expire_on_commit=False) as s:
        result = build(s)
        s.commit()
        return result


def legs_of(engine) -> list[Leg]:
    with Session(engine) as s:
        return list(s.scalars(select(Leg).order_by(Leg.id)))


def test_an_exact_main_line_is_recorded_with_its_source(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total("42.5")]))
    assert capture(NOW) == 1
    (leg,) = legs_of(engine)
    assert (leg.closing_line, leg.closing_odds, leg.closing_opposite_odds) == (
        D("42.5"), -108, -112)
    assert (leg.closing_source, leg.closing_captured_at) == (ClosingSource.ODDS_API, NOW)
    # One free events call to resolve the game, then one paid call for one market.
    assert odds.calls == [("events", Sport.NFL), ("odds", GAME, ["totals"])]
    assert odds.quota_remaining == 399
    with Session(engine) as s:
        assert s.scalars(select(Event.odds_api_event_id)).one() == GAME


def test_a_second_run_makes_no_calls_once_everything_is_captured(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    capture(NOW)
    calls = len(odds.calls)
    assert capture(NOW + timedelta(minutes=1)) == 0
    assert len(odds.calls) == calls


def test_nothing_due_costs_no_api_calls(engine, job):
    capture, odds = job
    assert capture(NOW) == 0
    assert odds.calls == []


def test_only_games_starting_within_five_minutes_are_captured(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    assert capture(KICKOFF - timedelta(minutes=6)) == 0  # too early
    assert capture(KICKOFF + timedelta(seconds=1)) == 0  # already started: too late
    assert odds.calls == []


def test_settled_other_and_already_captured_legs_are_skipped(engine, job):
    capture, odds = job

    def build(s):
        event = add_event(s)
        slip = add_slip(s, event, [total("42.5"), total("43.5")])
        svc.set_closing_line(s, slip.legs[0], closing_line=D("42.5"), closing_odds=-110)
        svc.settle_leg_manually(s, slip.legs[1], result=LegResult.WIN)
        add_slip(s, event, [{"market_type": "other", "description": "Eagles ML", "line": None}])

    commit(engine, build)
    assert capture(NOW) == 0
    assert odds.calls == []


def test_a_leg_missing_its_line_gets_the_alternate_market_in_one_follow_up(engine, job):
    capture, odds = job

    def build(s):
        event = add_event(s)
        add_slip(s, event, [total("42.5"), receptions("5.5"), receptions("4.5")])

    commit(engine, build)
    assert capture(NOW) == 3
    total_leg, main_leg, alt_leg = legs_of(engine)
    assert (main_leg.closing_odds, main_leg.closing_opposite_odds) == (-120, -106)
    assert (alt_leg.closing_line, alt_leg.closing_odds, alt_leg.closing_opposite_odds) == (
        D("4.5"), -248, None)
    assert odds.odds_calls == [
        ("odds", GAME, ["player_receptions", "totals"]),  # main markets, sorted
        ("odds", GAME, ["player_receptions_alternate"]),  # one follow-up, only what's needed
    ]
    assert odds.quota_remaining == 400 - 2 - 1


def test_a_line_the_book_never_offered_falls_back_to_its_main_line(engine, job):
    capture, odds = job
    # 6.5 is in DraftKings' alternate market; 5.0 is in neither market.
    commit(engine, lambda s: add_slip(
        s, add_event(s), [receptions("6.5"), receptions("5.0")]))
    assert capture(NOW) == 2
    alternate, fallback = legs_of(engine)
    assert (alternate.closing_line, alternate.closing_odds) == (D("6.5"), 153)
    # The main line, not the bet's: this gives line CLV only.
    assert (fallback.closing_line, fallback.closing_odds) == (D("5.5"), -120)


def test_a_sportsbook_the_api_does_not_return_falls_back_to_the_median(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total("42.5")], book="Caesars"))
    capture(NOW)
    (leg,) = legs_of(engine)
    assert (leg.closing_line, leg.closing_odds, leg.closing_source) == (
        D("42.5"), -110, ClosingSource.ODDS_API)
    assert len(odds.odds_calls) == 1  # no follow-up: Caesars is not in the response


def test_the_credit_reserve_is_never_spent(engine, job):
    capture, odds = job
    odds.quota_remaining = 51  # one market would leave 50: allowed; two would leave 49
    commit(engine, lambda s: add_slip(s, add_event(s), [total(), receptions()]))
    assert capture(NOW) == 0
    assert odds.odds_calls == []
    assert odds.quota_remaining == 51
    assert capture(NOW + timedelta(seconds=30)) == 0
    assert odds.odds_calls == []  # not retried every minute either


def test_exactly_the_reserve_is_allowed(engine, job):
    capture, odds = job
    odds.quota_remaining = 51
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    assert capture(NOW) == 1
    assert odds.quota_remaining == 50


def test_an_unknown_quota_is_allowed_through(engine, job):
    capture, odds = job
    odds.quota_remaining = None
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    assert capture(NOW) == 1


def test_a_game_the_api_does_not_list_is_left_for_manual_entry(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(
        s, add_event(s, home="Green Bay Packers", away="Detroit Lions"), [total()]))
    assert capture(NOW) == 0
    assert odds.odds_calls == []
    assert legs_of(engine)[0].closing_captured_at is None
    capture(NOW + timedelta(seconds=30))
    assert [c[0] for c in odds.calls] == ["events"]  # asked once, not every minute


def test_a_failed_call_is_retried_on_the_next_tick(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    odds.fail_next = [FetchError("u", FailureKind.TRANSIENT, "timeout")]
    assert capture(NOW) == 0
    assert legs_of(engine)[0].closing_captured_at is None
    assert capture(NOW + timedelta(minutes=1)) == 1


def test_an_open_breaker_skips_quietly_and_recovers(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    odds.fail_next = [ProviderOpen("odds_api", None)]
    assert capture(NOW) == 0
    assert capture(NOW + timedelta(minutes=1)) == 1


def test_a_rejected_request_is_logged_not_raised(engine, job):
    capture, odds = job

    def build(s):
        add_slip(s, add_event(s), [total()])

    commit(engine, build)
    odds.fail_next = [RequestRejected("HTTP 422")]
    assert capture(NOW) == 0  # logged, not raised
    assert legs_of(engine)[0].closing_captured_at is None


def test_the_follow_up_failing_keeps_the_first_calls_results(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total("42.5"), receptions("4.5")]))
    original = odds.event_odds

    def second_call_fails(sport, event_id, markets):
        if any(m.endswith("_alternate") for m in markets):
            raise FetchError("u", FailureKind.TRANSIENT, "timeout")
        return original(sport, event_id, markets)

    odds.event_odds = second_call_fails
    assert capture(NOW) == 2
    first, second = legs_of(engine)
    assert first.closing_odds == -108
    assert (second.closing_line, second.closing_odds) == (D("5.5"), -120)  # main line fallback


def test_a_response_that_does_not_fit_the_model_keeps_its_raw_body(engine, job):
    capture, odds = job
    commit(engine, lambda s: add_slip(s, add_event(s), [total()]))
    odds.fail_next = [FetchError("https://x/odds", FailureKind.SCHEMA, "bad", body='{"id": 1}')]
    assert capture(NOW) == 0
    with Session(engine) as s:
        sample = s.scalars(select(RawSample)).one()
    assert (sample.source, sample.reason, sample.body) == ("odds_api", "failure", '{"id": 1}')


def test_two_games_are_captured_independently(engine, job):
    capture, odds = job

    def build(s):
        add_slip(s, add_event(s), [total()])
        other = add_event(s, start=KICKOFF + timedelta(minutes=1), home="Green Bay Packers",
                          away="Detroit Lions", espn_id="401900002")
        add_slip(s, other, [total()])

    commit(engine, build)
    assert capture(NOW) == 1  # the second game isn't in the API's list
    first, second = legs_of(engine)
    assert first.closing_captured_at is not None and second.closing_captured_at is None

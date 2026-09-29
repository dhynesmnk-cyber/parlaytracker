"""The Live page through streamlit.testing, on seeded data and a fixed clock (SPEC.md 9.4)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from parlaytracker.app.pages import live as page
from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import (
    DataSource,
    Event,
    EventStatus as S,
    FailureKind,
    HealthState,
    Leg,
    LegResult,
    SourceHealth,
    Sport,
    Sportsbook,
)
from parlaytracker.core.schemas import SlipIn

pytestmark = pytest.mark.db

KICKOFF = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
NOW = KICKOFF + timedelta(hours=1, minutes=12)   # 2:12 pm ET, deep in the second quarter


def _live_page():
    from parlaytracker.app.pages import live

    live.render()


@pytest.fixture
def clock(app_env):
    app_env.setattr(page, "now", lambda: NOW)
    return NOW


def run() -> AppTest:
    at = AppTest.from_function(_live_page, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def text(at: AppTest) -> str:
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption]
                     + [e.value for e in at.error] + [i.value for i in at.info]
                     + [h.value for h in at.subheader])


def game(session, espn_id="401900001", sport=Sport.NFL, **fields) -> Event:
    event = services.upsert_event(
        session, sport=sport, espn_event_id=espn_id, start_time=fields.pop("start", KICKOFF),
        home_team="Chicago Bears", away_team="Philadelphia Eagles", home_espn_team_id="3",
        away_espn_team_id="21", status=S.SCHEDULED)
    for key, value in fields.items():
        setattr(event, key, value)
    session.flush()
    return event


def slip(session, event, legs, **kw):
    book = session.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
    single = len(legs) == 1
    data = {"is_placed": True, "stake": "10.00", "slip_type": "single" if single else "sgp",
            "sportsbook_id": book.id, "american_odds": -110, "source": "quick_add",
            "legs": [{"event_id": event.id, "american_odds": -110 if single else None, **leg}
                     for leg in legs], **kw}
    return services.create_slip(session, SlipIn(**data), "alice@example.com")


def total(line):
    return {"market_type": "game_total", "line": line}


def in_play(session, **fields):
    defaults = dict(status=S.IN_PROGRESS, period=2, clock_seconds=522, home_score=17,
                    away_score=10, last_polled_at=NOW - timedelta(seconds=12),
                    last_progress_at=NOW - timedelta(seconds=12))
    return game(session, **{**defaults, **fields})


# --- Empty and simple -------------------------------------------------------------------------


def test_nothing_to_watch_says_so(clock):
    at = run()
    assert any("Nothing to watch" in i.value for i in at.info)
    assert "Settled in the last 7 days" in text(at)


def test_a_single_shows_its_live_value_over_or_under_and_the_clock(clock):
    with session_scope() as s:
        event = in_play(s)
        one = slip(s, event, [total("25.5")])
        one.legs[0].live_value = D("27")
        one.legs[0].live_source = DataSource.ESPN_WEB
    out = text(run())
    assert "27" in out and "over" in out and "Q2 8:42" in out
    assert "updated 12 s ago · via espn_web" in out


def test_a_leg_under_its_line_says_under(clock):
    with session_scope() as s:
        event = in_play(s)
        one = slip(s, event, [total("45.5")])
        one.legs[0].live_value = D("27")
    assert "under" in text(run())


def test_the_score_gives_a_team_leg_its_value_before_the_worker_writes_one(clock):
    with session_scope() as s:
        event = in_play(s)   # 17 - 10 to the home team
        slip(s, event, [{"market_type": "alt_spread", "side": "home", "line": "-6.5"}])
    out = text(run())
    assert "7" in out and "covering" in out    # a margin of 7 covers -6.5


def test_a_player_with_no_stat_line_yet_is_not_shown_as_zero(clock):
    with session_scope() as s:
        event = in_play(s)
        slip(s, event, [{"market_type": "player_receptions", "espn_athlete_id": "7",
                         "line": "5.5"}])
    assert "no stat line yet" in text(run())


# --- Parlays and untracked legs ---------------------------------------------------------------


def test_a_parlay_shows_progress_and_marks_untracked_legs(clock):
    with session_scope() as s:
        nfl = in_play(s)
        nba = game(s, "500", sport=Sport.NBA, start=KICKOFF + timedelta(hours=8))
        book = s.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
        parlay = services.create_slip(s, SlipIn(
            is_placed=True, stake="10.00", slip_type="parlay", sportsbook_id=book.id,
            american_odds=264, source="quick_add",
            legs=[{"event_id": nfl.id, "market_type": "game_total", "line": "20.5",
                   "american_odds": -110},
                  {"event_id": nba.id, "market_type": "game_total", "line": "210.5",
                   "american_odds": -110}]), "alice@example.com")
        parlay.legs[0].live_value = D("27")
    out = text(run())
    assert "Not tracked live: settles after the game" in out
    assert "1 of 2 over" in out


def test_a_parlay_with_a_lost_leg_has_lost_so_it_leaves_the_live_view(clock):
    """Any lost leg loses the slip at once (section 7.2): it is no longer pending, so it is
    settled history, not something to watch. Its other legs still settle later."""
    with session_scope() as s:
        event = in_play(s)
        book = s.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
        parlay = services.create_slip(s, SlipIn(
            is_placed=True, stake="10.00", slip_type="sgp", sportsbook_id=book.id,
            american_odds=450, source="quick_add",
            legs=[{"event_id": event.id, "market_type": "game_total", "line": "20.5"},
                  {"event_id": event.id, "market_type": "game_total", "line": "60.5"}]),
            "alice@example.com")
        services.settle_leg_manually(s, parlay.legs[1], result=LegResult.LOSS)
    at = run()
    assert any("Nothing to watch" in i.value for i in at.info)   # no card
    assert "**loss**" in text(at)                                # but it is in Settled


def test_a_slip_with_no_active_nfl_game_has_no_card(clock):
    with session_scope() as s:
        tomorrow = game(s, start=KICKOFF + timedelta(days=1), status=S.SCHEDULED)
        slip(s, tomorrow, [total("45.5")])
    at = run()
    assert any("Nothing to watch" in i.value for i in at.info)


# --- Freshness colours and warnings -----------------------------------------------------------


@pytest.mark.parametrize(("age", "colour"), [
    (timedelta(seconds=30), ":green["), (timedelta(minutes=3), ":orange["),
    (timedelta(minutes=6), ":red[")])
def test_the_updated_line_turns_amber_after_2_minutes_and_red_after_5(clock, age, colour):
    with session_scope() as s:
        event = in_play(s, last_polled_at=NOW - age, last_progress_at=NOW - age)
        slip(s, event, [total("25.5")])
    assert colour + "updated" in text(run())


def test_no_change_for_n_minutes_shows_in_amber(clock):
    with session_scope() as s:
        event = in_play(s, last_progress_at=NOW - timedelta(minutes=9, seconds=30))
        slip(s, event, [total("25.5")])
    assert ":orange[No change for 9 min]" in text(run())


def test_a_game_that_is_on_but_recently_changed_has_no_no_change_line(clock):
    with session_scope() as s:
        slip(s, in_play(s), [total("25.5")])
    assert "No change" not in text(run())


def test_halftime_is_not_reported_as_no_change(clock):
    with session_scope() as s:
        event = in_play(s, status=S.BREAK, clock_seconds=0,
                        last_progress_at=NOW - timedelta(minutes=12))
        slip(s, event, [total("25.5")])
    out = text(run())
    assert "Halftime" in out and "No change" not in out


def test_when_every_espn_breaker_is_open_the_page_says_live_data_is_unavailable(clock):
    with session_scope() as s:
        slip(s, in_play(s, last_polled_at=NOW - timedelta(minutes=8)), [total("25.5")])
        failed = NOW - timedelta(minutes=8)
        for source in ("espn_web", "espn_site", "espn_cdn"):
            s.add(SourceHealth(source=source, state=HealthState.OPEN,
                               failure_kind=FailureKind.BLOCKED, last_failure_at=failed,
                               open_until=NOW + timedelta(minutes=2)))
    at = run()
    assert any("Live data unavailable since" in e.value and "Results will still settle"
               in e.value for e in at.error)
    assert ":red[updated" in text(at)


def test_one_working_provider_means_no_unavailable_message(clock):
    with session_scope() as s:
        slip(s, in_play(s), [total("25.5")])
        for source, state in (("espn_web", HealthState.OPEN), ("espn_site", HealthState.OK),
                              ("espn_cdn", HealthState.OPEN)):
            s.add(SourceHealth(source=source, state=state, last_failure_at=NOW))
    assert not run().error


# --- Settled in the last 7 days ---------------------------------------------------------------


def test_settled_slips_show_their_result_and_verification(clock):
    with session_scope() as s:
        event = game(s, status=S.FINAL, start=NOW - timedelta(hours=10))
        verified = slip(s, event, [total("20.5")])
        services.settle_leg_manually(s, verified.legs[0], final_value=D("30"), now=NOW)
        verified.legs[0].settlement_source = DataSource.ESPN_WEB
        verified.legs[0].verified_at, verified.legs[0].verified_source = NOW, DataSource.NFLVERSE
        pending = slip(s, event, [total("21.5")])
        services.settle_leg_manually(s, pending.legs[0], final_value=D("30"), now=NOW)
        pending.legs[0].settlement_source = DataSource.ESPN_WEB
        one_source = slip(s, event, [total("22.5")])
        services.settle_leg_manually(s, one_source.legs[0], final_value=D("30"), now=NOW)
        one_source.legs[0].settlement_source = DataSource.NFLVERSE
    out = text(run())
    assert "verified" in out and "awaiting verification" in out and "unverified" in out
    assert out.count("win") >= 3


def test_a_slip_settled_more_than_a_week_ago_is_not_listed(clock):
    with session_scope() as s:
        event = game(s, status=S.FINAL, start=NOW - timedelta(days=9))
        old = slip(s, event, [total("20.5")])
        services.settle_leg_manually(s, old.legs[0], final_value=D("30"),
                                     now=NOW - timedelta(days=8))
    assert "Nothing has settled in the last 7 days" in text(run())


def test_live_values_never_change_a_legs_result_on_this_page(clock):
    with session_scope() as s:
        event = in_play(s)
        one = slip(s, event, [total("10.5")])   # 27 is far past 10.5, but it is not settled
        one.legs[0].live_value = D("27")
    run()
    with session_scope() as s:
        assert s.scalars(select(Leg)).one().result is LegResult.PENDING


# --- Formatting -------------------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "text_"), [
    (None, "never"), (0, "0 s ago"), (42, "42 s ago"), (119, "119 s ago"), (120, "2 min ago"),
    (3599, "59 min ago")])
def test_age_text(seconds, text_):
    assert page.fmt_age(seconds) == text_


@pytest.mark.parametrize(("value", "text_"), [(None, "—"), (D("27"), "27"), (D("27.5"), "27.5"),
                                              (D("0.0"), "0")])
def test_value_text(value, text_):
    assert page.fmt_value(value) == text_


def test_the_page_is_in_the_navigation(app_env):
    from pathlib import Path

    main = Path(__file__).resolve().parents[2] / "parlaytracker" / "app" / "main.py"
    at = AppTest.from_file(str(main), default_timeout=30).run()
    assert not at.exception and '"Live"' in main.read_text()

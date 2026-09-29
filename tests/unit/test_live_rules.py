"""Live tracking rules against exact numbers (SPEC.md sections 8.1, 8.3 and 9.4)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from parlaytracker.core import live
from parlaytracker.core.live import LegView, Severity, State, Verification
from parlaytracker.core.models import (
    DataSource,
    EventStatus as S,
    HealthState,
    LegResult as R,
    MarketType as M,
    Sport,
    TeamSide,
)

KICKOFF = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)  # Sunday 1 pm ET
NOW = KICKOFF + timedelta(hours=1)


def event(status=S.IN_PROGRESS, start=KICKOFF, sport=Sport.NFL, **kw):
    defaults = dict(id=1, sport=sport, status=status, start_time=start, period=2,
                    clock_seconds=522, home_score=10, away_score=7, last_polled_at=None,
                    last_progress_at=None)
    return SimpleNamespace(**{**defaults, **kw})


def leg(market=M.GAME_TOTAL, line="45.5", side=None, result=R.PENDING, **kw):
    defaults = dict(id=1, market_type=market, line=D(line), side=side, result=result,
                    live_value=None, live_source=None, event=event(), verified_at=None,
                    settlement_source=None)
    return SimpleNamespace(**{**defaults, **kw})


# --- Which events are tracked -----------------------------------------------------------------


@pytest.mark.parametrize(("delta", "active"), [
    (timedelta(minutes=-31), False),   # more than 30 minutes before kickoff
    (timedelta(minutes=-30), True),
    (timedelta(0), True),
    (timedelta(hours=8), True),
    (timedelta(hours=8, seconds=1), False),
])
def test_an_event_is_active_from_30_minutes_before_to_8_hours_after_kickoff(delta, active):
    assert live.is_active(event(S.SCHEDULED), KICKOFF + delta) is active


@pytest.mark.parametrize("status", [S.FINAL, S.POSTPONED, S.CANCELLED])
def test_a_finished_or_called_off_event_is_not_active(status):
    assert not live.is_active(event(status), NOW)


@pytest.mark.parametrize("sport", [Sport.NBA, Sport.NHL, Sport.MLB])
def test_only_the_nfl_is_tracked_live(sport):
    assert not live.is_active(event(sport=sport), NOW)


# --- The cadence table (section 8.1) ----------------------------------------------------------


@pytest.mark.parametrize(("status", "scoreboard", "box"), [
    (S.SCHEDULED, timedelta(minutes=5), None),           # no box score before kickoff
    (S.IN_PROGRESS, timedelta(seconds=30), timedelta(seconds=60)),
    (S.BREAK, timedelta(minutes=2), timedelta(minutes=3)),
    (S.DELAYED, timedelta(minutes=2), timedelta(minutes=3)),
    (S.FINAL, None, None),                               # stop: settle takes over
    (S.POSTPONED, None, None),
    (S.CANCELLED, None, None),
])
def test_the_cadence_table(status, scoreboard, box):
    assert live.scoreboard_interval(status) == scoreboard
    assert live.box_interval(status) == box


# --- Live values and whether a leg is over or under -------------------------------------------


@pytest.mark.parametrize(("market", "side", "home", "away", "value"), [
    (M.GAME_TOTAL, None, 24, 17, 41),
    (M.TEAM_TOTAL, TeamSide.HOME, 24, 17, 24),
    (M.TEAM_TOTAL, TeamSide.AWAY, 24, 17, 17),
    (M.ALT_SPREAD, TeamSide.HOME, 24, 17, 7),     # the side's margin
    (M.ALT_SPREAD, TeamSide.AWAY, 24, 17, -7),
])
def test_a_score_leg_live_value(market, side, home, away, value):
    assert live.score_live_value(leg(market, side=side), home, away) == D(value)


def test_no_score_yet_and_player_markets_have_no_score_value():
    assert live.score_live_value(leg(), None, None) is None
    assert live.score_live_value(leg(M.PLAYER_RECEPTIONS), 10, 7) is None


@pytest.mark.parametrize(("market", "line", "value", "state"), [
    (M.GAME_TOTAL, "45.5", "46", State.OVER),
    (M.GAME_TOTAL, "45.5", "45", State.UNDER),
    (M.GAME_TOTAL, "45", "45", State.LEVEL),                  # a push if it stayed
    (M.PLAYER_RECEPTIONS, "5.5", "6", State.OVER),
    (M.PLAYER_RECEIVING_YARDS, "70.5", "0", State.UNDER),
    (M.ALT_SPREAD, "-7.5", "8", State.OVER),                  # covering: 8 - 7.5 > 0
    (M.ALT_SPREAD, "-7.5", "7", State.UNDER),
    (M.ALT_SPREAD, "+3.5", "-3", State.OVER),
    (M.ALT_SPREAD, "+3.5", "-4", State.UNDER),
    (M.ALT_SPREAD, "-7", "7", State.LEVEL),
])
def test_over_or_under_right_now_uses_the_settlement_rules(market, line, value, state):
    assert live.leg_state(market, D(line), D(value)) is state


def test_no_value_yet_is_unknown_not_under():
    assert live.leg_state(M.PLAYER_RECEPTIONS, D("5.5"), None) is State.UNKNOWN
    assert live.leg_state(M.OTHER, D("5.5"), D("9")) is State.UNKNOWN


# --- Freshness: green, amber after 2 minutes, red after 5 (section 9.4) -----------------------


@pytest.mark.parametrize(("age", "severity"), [
    (timedelta(seconds=0), Severity.OK), (timedelta(minutes=2), Severity.OK),
    (timedelta(minutes=2, seconds=1), Severity.AMBER), (timedelta(minutes=5), Severity.AMBER),
    (timedelta(minutes=5, seconds=1), Severity.RED), (timedelta(hours=1), Severity.RED)])
def test_freshness_while_in_progress(age, severity):
    assert live.freshness(S.IN_PROGRESS, NOW - age, NOW) is severity


def test_never_polled_while_in_play_is_red():
    assert live.freshness(S.IN_PROGRESS, None, NOW) is Severity.RED


@pytest.mark.parametrize("status", [S.SCHEDULED, S.FINAL, S.POSTPONED])
def test_a_game_not_in_play_is_never_coloured(status):
    assert live.freshness(status, NOW - timedelta(hours=3), NOW) is Severity.OK


@pytest.mark.parametrize(("age", "severity"), [
    (timedelta(minutes=2), Severity.OK),      # the normal 2-minute cadence is fine
    (timedelta(minutes=4), Severity.OK),
    (timedelta(minutes=4, seconds=1), Severity.AMBER),
    (timedelta(minutes=7, seconds=1), Severity.RED)])
@pytest.mark.parametrize("status", [S.BREAK, S.DELAYED])
def test_a_break_or_delay_is_polled_slower_so_its_limits_are_longer(status, age, severity):
    assert live.freshness(status, NOW - age, NOW) is severity


@pytest.mark.parametrize(("age", "minutes"), [
    (timedelta(minutes=5), None), (timedelta(minutes=5, seconds=1), 5),
    (timedelta(minutes=9, seconds=59), 9), (timedelta(minutes=12), 12)])
def test_no_change_for_n_minutes_after_five(age, minutes):
    assert live.no_change_minutes(S.IN_PROGRESS, NOW - age, NOW) == minutes


@pytest.mark.parametrize("status", [S.BREAK, S.DELAYED, S.SCHEDULED, S.FINAL])
def test_a_stopped_clock_is_only_news_while_in_progress(status):
    assert live.no_change_minutes(status, NOW - timedelta(minutes=30), NOW) is None


def test_age_in_seconds():
    assert live.age_seconds(NOW - timedelta(seconds=42), NOW) == 42
    assert live.age_seconds(None, NOW) is None
    assert live.age_seconds(NOW + timedelta(seconds=5), NOW) == 0  # never negative


# --- Status text ------------------------------------------------------------------------------


@pytest.mark.parametrize(("period", "text"), [
    (None, ""), (0, ""), (1, "Q1"), (4, "Q4"), (5, "OT"), (6, "2OT")])
def test_period_labels(period, text):
    assert live.period_label(period) == text


@pytest.mark.parametrize(("seconds", "text"), [(None, ""), (0, "0:00"), (522, "8:42"),
                                               (900, "15:00"), (65, "1:05")])
def test_clock_text(seconds, text):
    assert live.clock_text(seconds) == text


@pytest.mark.parametrize(("ev", "text"), [
    (event(S.IN_PROGRESS, period=3, clock_seconds=522), "Q3 8:42"),
    (event(S.IN_PROGRESS, period=5, clock_seconds=300), "OT 5:00"),
    (event(S.IN_PROGRESS, period=None, clock_seconds=None), "In progress"),
    (event(S.BREAK, period=2), "Halftime"),
    (event(S.BREAK, period=1), "End of Q1"),
    (event(S.DELAYED), "Delayed"),
    (event(S.SCHEDULED, start=NOW + timedelta(minutes=12)), "Kickoff in 12 min"),
    (event(S.SCHEDULED, start=NOW - timedelta(minutes=1)), "About to start"),
    (event(S.FINAL), "Final"),
])
def test_game_status_text(ev, text):
    assert live.game_status_text(ev, NOW) == text


# --- Parlay progress --------------------------------------------------------------------------


def view(state=State.UNKNOWN, settled=None):
    return LegView(1, None, True, None, state, settled, "", None)


def test_progress_reads_2_of_4_over_1_lost():
    views = [view(State.OVER), view(State.OVER), view(State.UNDER), view(settled=R.LOSS)]
    assert live.progress_text(views) == "2 of 4 over, 1 lost"


def test_a_won_leg_counts_as_over_and_lost_is_left_out_when_none():
    assert live.progress_text([view(settled=R.WIN), view(State.OVER), view(State.UNDER)]) == \
        "2 of 3 over"


def test_a_leg_with_no_value_yet_is_not_counted_as_over():
    assert live.progress_text([view(State.UNKNOWN), view(State.OVER)]) == "1 of 2 over"


def test_a_single_has_no_progress_line():
    assert live.progress_text([view(State.OVER)]) is None


# --- ESPN unavailable -------------------------------------------------------------------------


def row(source, state, failed=None):
    return SimpleNamespace(source=source, state=state, last_failure_at=failed)


T = datetime(2026, 10, 4, 18, 30, tzinfo=UTC)


def test_unavailable_when_every_espn_breaker_is_open_since_the_last_one_failed():
    rows = [row("espn_web", HealthState.OPEN, T), row("espn_site", HealthState.OPEN,
                                                      T + timedelta(minutes=1)),
            row("espn_cdn", HealthState.OPEN, T + timedelta(minutes=3)),
            row("worker", HealthState.OK), row("odds_api", HealthState.OK)]
    assert live.espn_unavailable_since(rows) == T + timedelta(minutes=3)


@pytest.mark.parametrize("working", ["espn_web", "espn_site", "espn_cdn"])
def test_one_working_provider_means_live_data_is_still_available(working):
    rows = [row(s, HealthState.OK if s == working else HealthState.OPEN, T)
            for s in ("espn_web", "espn_site", "espn_cdn")]
    assert live.espn_unavailable_since(rows) is None


def test_a_degraded_provider_is_not_unavailable_and_a_missing_row_is_not_proof():
    assert live.espn_unavailable_since([row("espn_web", HealthState.OPEN, T),
                                        row("espn_site", HealthState.OPEN, T)]) is None
    assert live.espn_unavailable_since([row("espn_web", HealthState.DEGRADED, T)]) is None
    assert live.espn_unavailable_since([]) is None


# --- Verified / awaiting / unverified (section 9.4, 7.1) --------------------------------------


SUNDAY_GAME = event(start=KICKOFF)
BEFORE_TUESDAY = KICKOFF + timedelta(days=1)
AFTER_TUESDAY = KICKOFF + timedelta(days=3)


def settled_leg(**kw):
    return leg(result=R.WIN, event=SUNDAY_GAME, settlement_source=DataSource.ESPN_WEB, **kw)


def test_verified_awaiting_and_unverified():
    verified = settled_leg(verified_at=NOW)
    assert live.verification(verified, AFTER_TUESDAY) is Verification.VERIFIED
    assert live.verification(settled_leg(), BEFORE_TUESDAY) is Verification.AWAITING
    assert live.verification(settled_leg(), AFTER_TUESDAY) is Verification.UNVERIFIED


@pytest.mark.parametrize("source", [DataSource.NFLVERSE, DataSource.MANUAL])
def test_a_leg_settled_from_one_source_can_never_be_verified_by_it(source):
    only = leg(result=R.WIN, event=SUNDAY_GAME, settlement_source=source)
    assert live.verification(only, BEFORE_TUESDAY) is Verification.UNVERIFIED


def test_other_sports_and_other_legs_have_no_label():
    nba = leg(result=R.WIN, event=event(sport=Sport.NBA), settlement_source=DataSource.ESPN_WEB)
    assert live.verification(nba, NOW) is Verification.NOT_APPLICABLE
    other = leg(M.OTHER, result=R.WIN, event=SUNDAY_GAME, settlement_source=DataSource.MANUAL)
    assert live.verification(other, NOW) is Verification.NOT_APPLICABLE

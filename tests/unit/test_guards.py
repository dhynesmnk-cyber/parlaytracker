"""Integrity guards: progress key, freshness, frozen feed and plausibility (SPEC.md 8.3)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from parlaytracker.core.models import EventStatus as S, MarketType as M, Sport
from parlaytracker.ingest import guards
from parlaytracker.ingest.guards import Verdict, compare, progress_key

NOW = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)


def key(status, period, clock, sport=Sport.NFL):
    return progress_key(sport, status, period, clock)


# --- Progress key ---------------------------------------------------------------------------


def test_status_ranks():
    assert key(S.SCHEDULED, 0, None)[0] == 0
    assert key(S.IN_PROGRESS, 1, 900)[0] == key(S.BREAK, 2, 0)[0] == key(S.DELAYED, 1, 500)[0] == 1
    assert key(S.FINAL, 4, 0)[0] == 2
    assert key(S.POSTPONED, None, None) is None and key(S.CANCELLED, None, None) is None


def test_seconds_elapsed_is_period_length_minus_clock():
    assert key(S.IN_PROGRESS, 1, 900) == (1, 1, 0)
    assert key(S.IN_PROGRESS, 1, 600) == (1, 1, 300)
    assert key(S.IN_PROGRESS, 2, 700, Sport.NBA) == (1, 2, 20)
    assert key(S.IN_PROGRESS, 2, 1000, Sport.NHL) == (1, 2, 200)


def test_overtime_is_a_later_period():
    assert key(S.IN_PROGRESS, 5, 600) > key(S.IN_PROGRESS, 4, 0)


def test_mlb_has_no_clock():
    assert key(S.IN_PROGRESS, 7, None, Sport.MLB) == (1, 7, 0)
    assert key(S.IN_PROGRESS, 8, 0, Sport.MLB) > key(S.IN_PROGRESS, 7, 0, Sport.MLB)


# --- Comparing with what is stored ----------------------------------------------------------


@pytest.mark.parametrize(("new", "stored", "verdict"), [
    ((1, 2, 100), (1, 2, 200), Verdict.STALE),          # a cached response, older
    ((1, 1, 899), (1, 2, 0), Verdict.STALE),            # an earlier period
    ((0, 0, 0), (1, 1, 10), Verdict.STALE),             # back to scheduled
    ((1, 2, 200), (1, 2, 200), Verdict.CORRECTION),     # equal: a correction if values differ
    ((1, 2, 201), (1, 2, 200), Verdict.ADVANCE),
    ((2, 4, 900), (1, 4, 800), Verdict.ADVANCE),        # final
    ((1, 1, 0), None, Verdict.ADVANCE),                 # nothing stored yet
])
def test_compare(new, stored, verdict):
    assert compare(new, stored) is verdict


def test_final_never_goes_back_to_play():
    for back in (S.SCHEDULED, S.IN_PROGRESS, S.BREAK, S.DELAYED):
        assert not guards.accept_status_change(S.FINAL, back)
    assert guards.accept_status_change(S.FINAL, S.FINAL)
    assert guards.accept_status_change(S.IN_PROGRESS, S.FINAL)
    assert guards.accept_status_change(S.POSTPONED, S.SCHEDULED)  # rescheduled


# --- Frozen feed ----------------------------------------------------------------------------


def test_frozen_only_when_in_progress_and_stale():
    old = NOW - timedelta(minutes=5, seconds=1)
    assert guards.is_frozen(S.IN_PROGRESS, old, NOW)
    assert not guards.is_frozen(S.IN_PROGRESS, NOW - timedelta(minutes=5), NOW)  # not *more*
    assert not guards.is_frozen(S.BREAK, old, NOW)     # halftime: the clock rightly stands
    assert not guards.is_frozen(S.DELAYED, old, NOW)   # a weather delay likewise
    assert not guards.is_frozen(S.FINAL, old, NOW)
    assert not guards.is_frozen(S.IN_PROGRESS, None, NOW)


def test_probe_at_most_once_every_five_minutes():
    assert guards.may_probe(None, NOW)
    assert not guards.may_probe(NOW - timedelta(minutes=4, seconds=59), NOW)
    assert guards.may_probe(NOW - timedelta(minutes=5), NOW)


# --- Plausibility ---------------------------------------------------------------------------


@pytest.mark.parametrize(("market", "ok", "bad"), [
    (M.PLAYER_RECEIVING_YARDS, (-30, 400), (-31, 401)),
    (M.PLAYER_RUSHING_YARDS, (-30, 400), (-31, 401)),
    (M.PLAYER_PASSING_YARDS, (-30, 700), (-31, 701)),
    (M.PLAYER_RECEPTIONS, (0, 25), (-1, 26)),
])
def test_stat_bounds(market, ok, bad):
    for v in ok:
        guards.check_stats({market: {"1": D(v)}})
    for v in bad:
        with pytest.raises(guards.ImplausibleError):
            guards.check_stats({market: {"1": D(v)}})


def test_points_have_no_bounds_in_the_spec():
    guards.check_stats({M.PLAYER_POINTS: {"1": D(200)}})


def test_nfl_team_score_bounds():
    guards.check_score(Sport.NFL, 0)
    guards.check_score(Sport.NFL, 99)
    guards.check_score(Sport.NFL, None)
    for bad in (-1, 100):
        with pytest.raises(guards.ImplausibleError):
            guards.check_score(Sport.NFL, bad)
    guards.check_score(Sport.NBA, 137)  # other sports' scores aren't bounded here

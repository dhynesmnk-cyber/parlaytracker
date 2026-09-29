import pytest

from parlaytracker.core.models import MarketType, Sport
from parlaytracker.core.markets import MARKET_SPORTS
from parlaytracker.ingest.resolve import (
    MARKET_KEYS,
    SPORT_KEYS,
    normalize_name,
    same_game,
    same_player,
    same_team,
)
from datetime import UTC, datetime, timedelta


def test_every_analysed_market_and_sport_has_a_key():
    assert set(MARKET_KEYS) == set(MARKET_SPORTS) - {MarketType.OTHER}
    assert set(SPORT_KEYS) == set(Sport)


@pytest.mark.parametrize(("a", "b"), [
    ("Luther Burden III", "Luther Burden"),
    ("D'Andre Swift", "DAndre Swift"),
    ("Amon-Ra St. Brown", "Amon Ra St Brown"),
    ("Kenneth Walker III", "Kenneth Walker"),
    ("Marvin Harrison Jr.", "Marvin Harrison"),
    ("Devonta Smith", "DeVonta Smith"),
])
def test_same_player(a, b):
    assert same_player(a, b)


@pytest.mark.parametrize(("a", "b"), [
    ("Jalen Hurts", "Jalen Carter"),
    ("Josh Allen", "Josh Jacobs"),
    ("A.J. Brown", "AJ Dillon"),
])
def test_different_players(a, b):
    assert not same_player(a, b)


def test_normalize_name():
    assert normalize_name("Dalvin Cook-Éa Jr.") == "dalvin cook ea jr"


def test_same_team_ignores_case_and_punctuation():
    assert same_team("Chicago Bears", "chicago bears")
    assert not same_team("Chicago Bears", "Chicago Bulls")


def test_same_game_needs_both_teams_and_a_close_start():
    start = datetime(2026, 9, 29, 0, 15, tzinfo=UTC)
    args = ("Chicago Bears", "Philadelphia Eagles", start)
    assert same_game(*args, "Chicago Bears", "Philadelphia Eagles", start + timedelta(hours=3))
    assert not same_game(*args, "Chicago Bears", "Philadelphia Eagles",
                         start + timedelta(hours=3, minutes=1))
    assert not same_game(*args, "Philadelphia Eagles", "Chicago Bears", start)  # home/away swapped

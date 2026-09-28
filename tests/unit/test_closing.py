"""Closing-line selection against the real recorded Odds API response (SPEC.md section 8.2)."""
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from parlaytracker.core.models import MarketType, TeamSide
from parlaytracker.ingest import closing
from parlaytracker.ingest.closing import Basis, LegQuery
from parlaytracker.ingest.odds_api import EventOdds

FIXTURES = Path(__file__).parents[1] / "fixtures" / "odds_api"


@pytest.fixture(scope="module")
def event() -> EventOdds:
    return EventOdds.model_validate(
        json.loads((FIXTURES / "nfl_event_odds_2026-09-28_PHI-CHI.json").read_text()))


def leg(market: MarketType, line: str, *, side=None, player=None, book="draftkings") -> LegQuery:
    return LegQuery(market, D(line), side, player, book)


def test_exact_line_in_the_main_market(event):
    hit = closing.find_exact_main(leg(MarketType.GAME_TOTAL, "42.5"), event)
    assert (hit.line, hit.odds, hit.opposite_odds, hit.basis) == (
        D("42.5"), -108, -112, Basis.EXACT_MAIN)


def test_exact_line_in_the_alternate_market(event):
    q = leg(MarketType.GAME_TOTAL, "43.5")
    assert closing.find_exact_main(q, event) is None
    hit = closing.find_exact_alternate(q, event)
    assert (hit.line, hit.odds, hit.opposite_odds, hit.basis) == (
        D("43.5"), 103, -136, Basis.EXACT_ALTERNATE)


def test_player_match_ignores_case_and_punctuation(event):
    hit = closing.find_exact_main(
        leg(MarketType.PLAYER_RECEPTIONS, "5.5", player="Devonta Smith"), event)
    assert (hit.odds, hit.opposite_odds) == (-120, -106)


def test_player_alternate_can_lack_the_under(event):
    q = leg(MarketType.PLAYER_RECEPTIONS, "4.5", player="DeVonta Smith")
    assert closing.find_exact_main(q, event) is None
    hit = closing.find_exact_alternate(q, event)
    assert (hit.odds, hit.opposite_odds) == (-248, None)


def test_unknown_player_matches_nothing(event):
    q = leg(MarketType.PLAYER_RECEPTIONS, "5.5", player="Someone Else")
    assert closing.find_exact_main(q, event) is None
    assert closing.find_median_main(q, event) is None


def test_spread_uses_the_chosen_team_and_pairs_the_other_side(event):
    # The Bears are home; the main spread has them at +3.5.
    hit = closing.find_exact_main(
        leg(MarketType.ALT_SPREAD, "3.5", side=TeamSide.HOME), event)
    assert (hit.odds, hit.opposite_odds) == (-115, -105)
    # Alternate: Bears -3.5, paired with the Eagles at +3.5.
    hit = closing.find_exact_alternate(
        leg(MarketType.ALT_SPREAD, "-3.5", side=TeamSide.HOME), event)
    assert (hit.odds, hit.opposite_odds) == (223, -318)
    # The same number for the Eagles (away) is a different bet.
    hit = closing.find_exact_main(
        leg(MarketType.ALT_SPREAD, "-3.5", side=TeamSide.AWAY), event)
    assert hit.odds == -105


def test_team_total_needs_the_right_team(event):
    fanduel = leg(MarketType.TEAM_TOTAL, "19.5", side=TeamSide.HOME, book="fanduel")
    assert closing.find_exact_main(fanduel, event).odds == -113
    away = leg(MarketType.TEAM_TOTAL, "23.5", side=TeamSide.AWAY, book="fanduel")
    assert closing.find_exact_main(away, event).odds == -111


def test_a_different_line_falls_back_to_the_books_main_line(event):
    hit = closing.find_book_main(
        leg(MarketType.PLAYER_RECEPTIONS, "6.5", player="DeVonta Smith"), event)
    assert (hit.line, hit.odds, hit.basis) == (D("5.5"), -120, Basis.BOOK_MAIN)
    assert not hit.basis.exact


def test_median_main_line_across_books(event):
    # Five books at 42.5 and four at 42.0: the median line is 42.5, at -110 on both sides.
    hit = closing.find_median_main(leg(MarketType.GAME_TOTAL, "45", book=None), event)
    assert (hit.line, hit.odds, hit.opposite_odds, hit.basis) == (
        D("42.5"), -110, -110, Basis.MEDIAN_MAIN)


def test_a_book_with_no_odds_api_key_uses_the_median(event):
    caesars = leg(MarketType.PLAYER_RECEPTIONS, "5.5", player="DeVonta Smith",
                  book="williamhill_us")
    assert closing.find_exact_main(caesars, event) is None
    assert closing.find_book_main(caesars, event) is None
    assert not closing.needs_alternate_call(caesars, event)
    assert closing.find_median_main(caesars, event).line == D("5.5")


def test_median_of_prices_that_straddle_even_money():
    from parlaytracker.ingest.closing import _median_odds
    assert _median_odds([-105, 100, 105]) == 100
    assert _median_odds([-110, -110, -112]) == -110


def test_a_book_missing_from_the_response_needs_no_alternate_call(event):
    assert closing.needs_alternate_call(leg(MarketType.GAME_TOTAL, "42.5"), event)
    assert not closing.needs_alternate_call(
        leg(MarketType.GAME_TOTAL, "42.5", book="caesarsfake"), event)

"""Validation cases from SPEC.md section 11 ("Rejected by SlipIn / LegIn")."""
import pytest
from pydantic import ValidationError

from parlaytracker.core.schemas import ExtractedSlip, LegIn, SlipIn

GOOD_LEG = {"event_id": 1, "market_type": "game_total", "line": "45.5", "american_odds": -110}


def single(**overrides):
    data = {
        "is_placed": False,
        "slip_type": "single",
        "sportsbook_id": 1,
        "american_odds": -110,
        "source": "quick_add",
        "legs": [GOOD_LEG],
    }
    return SlipIn(**{**data, **overrides})


def test_valid_single():
    assert single().legs[0].line == 45.5


def test_valid_sgp_with_leg_without_odds():
    player_leg = {"event_id": 1, "market_type": "player_receptions", "espn_athlete_id": "9",
                  "line": "4.5"}
    s = single(is_placed=True, stake="10", slip_type="sgp", american_odds=450,
               legs=[GOOD_LEG, player_leg])
    assert s.legs[1].american_odds is None


def test_valid_other_leg_without_line():
    leg = LegIn(event_id=1, market_type="other", description="Under 45.5", american_odds=-110)
    assert leg.line is None


def test_valid_alt_spread_negative_line():
    leg = LegIn(event_id=1, market_type="alt_spread", side="home", line="-7.5", american_odds=120)
    assert leg.line < 0


LEG_REJECTIONS = {
    "line not whole or half": {**GOOD_LEG, "line": "45.3"},
    "odds between -99 and +99": {**GOOD_LEG, "american_odds": 50},
    "negative Over line": {**GOOD_LEG, "line": "-3.5"},
    "team market without side": {**GOOD_LEG, "market_type": "alt_spread", "line": "-7.5"},
    "side on non-team market": {**GOOD_LEG, "side": "home"},
    "player market without athlete": {**GOOD_LEG, "market_type": "player_points"},
    "athlete on non-player market": {**GOOD_LEG, "espn_athlete_id": "9"},
    "other leg without description": {"event_id": 1, "market_type": "other",
                                      "american_odds": -110},
    "non-other leg without line": {"event_id": 1, "market_type": "game_total",
                                   "american_odds": -110},
    "unknown market": {**GOOD_LEG, "market_type": "moneyline"},
}


@pytest.mark.parametrize("case", LEG_REJECTIONS)
def test_leg_rejections(case):
    with pytest.raises(ValidationError):
        LegIn(**LEG_REJECTIONS[case])


SLIP_REJECTIONS = {
    "placed without stake": {"is_placed": True},
    "single with two legs": {"legs": [GOOD_LEG, {**GOOD_LEG, "line": "47.5"}]},
    "single leg odds differ from slip odds": {"american_odds": -120},
    "parlay with one leg": {"slip_type": "parlay"},
    "parlay leg without odds": {"slip_type": "parlay", "american_odds": 264,
                                "legs": [GOOD_LEG, {**GOOD_LEG, "event_id": 2,
                                                    "american_odds": None}]},
    "sgp across two games": {"slip_type": "sgp", "american_odds": 264,
                             "legs": [GOOD_LEG, {**GOOD_LEG, "event_id": 2}]},
    "same selection twice": {"slip_type": "parlay", "american_odds": 264,
                             "legs": [GOOD_LEG, GOOD_LEG]},
    "slip odds between -99 and +99": {"american_odds": -99},
    "no legs": {"legs": []},
    "zero stake": {"is_placed": True, "stake": "0"},
}


@pytest.mark.parametrize("case", SLIP_REJECTIONS)
def test_slip_rejections(case):
    with pytest.raises(ValidationError):
        single(**SLIP_REJECTIONS[case])


def test_extracted_slip_accepts_partial_output():
    extracted = ExtractedSlip.model_validate_json('{"legs": [{"player_name": "X"}]}')
    assert extracted.legs[0].player_name == "X"
    assert extracted.american_odds is None

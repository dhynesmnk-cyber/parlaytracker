"""Every row of the settlement tables in SPEC.md sections 7.1 and 7.2."""
from decimal import Decimal

import pytest

from parlaytracker.core.models import LegResult, MarketType, SlipStatus, SlipType, TeamSide
from parlaytracker.core.settlement import (
    REDUCED_PARLAY_REASON,
    LegOutcome,
    SlipOutcome,
    score_value,
    settle_leg,
    settle_slip,
)

W, L, P, V, PEND = LegResult.WIN, LegResult.LOSS, LegResult.PUSH, LegResult.VOID, LegResult.PENDING

# --- 7.1 legs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("market", "line", "value", "expected"),
    [
        (MarketType.PLAYER_RUSHING_YARDS, "50.5", 51, W),
        (MarketType.PLAYER_RUSHING_YARDS, "50.5", 50, L),
        (MarketType.GAME_TOTAL, "45", 45, P),
        (MarketType.GAME_TOTAL, "45", 46, W),
        (MarketType.ALT_SPREAD, "-7.5", 8, W),
        (MarketType.ALT_SPREAD, "-7.5", 7, L),
        (MarketType.ALT_SPREAD, "3.5", -3, W),
        (MarketType.ALT_SPREAD, "3.5", -4, L),
        (MarketType.ALT_SPREAD, "-7", 7, P),
    ],
)
def test_settle_leg_table(market, line, value, expected):
    assert settle_leg(market, Decimal(line), value) is expected


def test_settle_leg_accepts_decimal_values():
    assert settle_leg(MarketType.PLAYER_RECEIVING_YARDS, Decimal("60.5"), Decimal("-3")) is L


def test_other_leg_is_never_auto_settled():
    with pytest.raises(ValueError):
        settle_leg(MarketType.OTHER, Decimal("1"), 1)


@pytest.mark.parametrize(
    ("market", "side", "expected"),
    [
        (MarketType.GAME_TOTAL, None, 51),
        (MarketType.TEAM_TOTAL, TeamSide.HOME, 27),
        (MarketType.TEAM_TOTAL, TeamSide.AWAY, 24),
        (MarketType.ALT_SPREAD, TeamSide.HOME, 3),
        (MarketType.ALT_SPREAD, TeamSide.AWAY, -3),
    ],
)
def test_score_value(market, side, expected):
    assert score_value(market, side, home_score=27, away_score=24) == expected


def test_score_value_rejects_player_market_and_missing_side():
    with pytest.raises(ValueError):
        score_value(MarketType.PLAYER_POINTS, None, 1, 2)
    with pytest.raises(ValueError):
        score_value(MarketType.TEAM_TOTAL, None, 1, 2)


# --- 7.2 slips --------------------------------------------------------------------------

TEN = Decimal("10.00")


def slip(slip_type, legs, *, is_placed=True, boosted=False, stake=TEN, odds=670):
    outcomes = [LegOutcome(result, leg_odds) for result, leg_odds in legs]
    return settle_slip(slip_type, is_placed, boosted, stake if is_placed else None, odds, outcomes)


def test_any_leg_lost_loses_immediately_even_with_pending_legs():
    out = slip(SlipType.PARLAY, [(W, -110), (L, 120), (PEND, -120)])
    assert out == SlipOutcome(SlipStatus.LOSS, Decimal("0.00"))


def test_pending_without_loss_stays_pending():
    assert slip(SlipType.PARLAY, [(W, -110), (PEND, 120)]) == SlipOutcome(SlipStatus.PENDING, None)


def test_all_won_pays_slip_odds():
    out = slip(SlipType.PARLAY, [(W, -110), (W, 120), (W, -120)], odds=670)
    assert out == SlipOutcome(SlipStatus.WIN, Decimal("77.00"))


def test_all_won_sgp_uses_slip_odds_not_legs():
    out = slip(SlipType.SGP, [(W, None), (W, None)], odds=450)
    assert out == SlipOutcome(SlipStatus.WIN, Decimal("55.00"))


def test_single_win():
    assert slip(SlipType.SINGLE, [(W, -110)], odds=-110).payout == Decimal("19.09")


@pytest.mark.parametrize(("leg", "status"), [(P, SlipStatus.PUSH), (V, SlipStatus.VOID)])
def test_single_push_or_void_returns_stake(leg, status):
    assert slip(SlipType.SINGLE, [(leg, -110)], odds=-110) == SlipOutcome(status, TEN)


@pytest.mark.parametrize("slip_type", [SlipType.PARLAY, SlipType.SGP])
def test_all_legs_push_or_void_is_void(slip_type):
    assert slip(slip_type, [(P, -110), (V, 120)]) == SlipOutcome(SlipStatus.VOID, TEN)


def test_reduced_parlay_reprices_from_winning_legs():
    # Worked example: the +120 leg pushes, leaving -110 and -120 -> 3.50 -> $35.00.
    out = slip(SlipType.PARLAY, [(W, -110), (P, 120), (W, -120)], odds=670)
    assert out == SlipOutcome(SlipStatus.WIN, Decimal("35.00"))


@pytest.mark.parametrize(
    ("slip_type", "boosted"), [(SlipType.SGP, False), (SlipType.PARLAY, True)]
)
def test_reduced_sgp_or_boosted_needs_review_when_placed(slip_type, boosted):
    out = slip(slip_type, [(W, -110), (V, 120), (W, -120)], boosted=boosted)
    assert out == SlipOutcome(SlipStatus.PENDING, None, REDUCED_PARLAY_REASON)


@pytest.mark.parametrize(
    ("slip_type", "boosted"), [(SlipType.SGP, False), (SlipType.PARLAY, True)]
)
def test_reduced_sgp_or_boosted_is_void_when_unplaced(slip_type, boosted):
    out = slip(slip_type, [(W, -110), (V, 120), (W, -120)], boosted=boosted, is_placed=False)
    assert out == SlipOutcome(SlipStatus.VOID, Decimal("1.00"))


def test_unplaced_slips_settle_in_units():
    assert slip(SlipType.SINGLE, [(W, -110)], odds=-110, is_placed=False).payout == Decimal("1.91")
    assert slip(SlipType.SINGLE, [(L, -110)], odds=-110, is_placed=False).payout == Decimal("0.00")


def test_invalid_inputs():
    with pytest.raises(ValueError):
        settle_slip(SlipType.SINGLE, True, False, TEN, -110, [])
    with pytest.raises(ValueError):
        settle_slip(SlipType.SINGLE, True, False, None, -110, [LegOutcome(W, -110)])

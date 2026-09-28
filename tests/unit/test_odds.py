from decimal import Decimal

import pytest

from parlaytracker.core.odds import (
    american_odds,
    decimal_odds,
    implied_probability,
    no_vig,
    parlay_decimal,
    payout,
    wilson_interval,
)

# Cases ported from the previous tests/test_math_calculator.py.


@pytest.mark.parametrize(
    ("american", "expected"),
    [(100, "2.0"), (150, "2.5"), (200, "3.0"), (300, "4.0"), (-200, "1.5")],
)
def test_decimal_odds_exact(american, expected):
    assert decimal_odds(american) == Decimal(expected)


@pytest.mark.parametrize(("american", "expected"), [(-110, 1.909), (-120, 1.833), (-150, 1.667)])
def test_decimal_odds_negative(american, expected):
    assert float(decimal_odds(american)) == pytest.approx(expected, abs=5e-4)


def test_parlay_two_legs():
    assert float(parlay_decimal([100, -110])) == pytest.approx(3.818, abs=5e-3)


def test_parlay_three_legs():
    assert float(parlay_decimal([-110, -110, -110])) == pytest.approx(6.96, abs=5e-3)


def test_parlay_payout_full():
    multiplier = parlay_decimal([150, -110])
    assert float(multiplier) == pytest.approx(4.773, abs=5e-3)
    total = payout(Decimal("100"), multiplier)
    assert total == Decimal("477.27")
    assert total - Decimal("100") == Decimal("377.27")


def test_parlay_even_money():
    assert parlay_decimal([100, 100]) == Decimal(4)
    assert payout(Decimal("100"), parlay_decimal([100, 100])) == Decimal("400.00")


# SPEC.md section 7.2 worked example and 7.3 values.


def test_worked_example_parlay():
    d = parlay_decimal([-110, 120, -120])
    assert american_odds(d) == 670
    assert payout(Decimal("10"), d) == Decimal("77.00")


def test_worked_example_after_push():
    d = parlay_decimal([-110, -120])
    assert american_odds(d) == 250
    assert payout(Decimal("10"), d) == Decimal("35.00")


def test_single_payout_rounds_to_cent():
    assert payout(Decimal("10"), decimal_odds(-110)) == Decimal("19.09")


@pytest.mark.parametrize("american", [*range(-500, -100), *range(100, 501)])
def test_american_round_trip(american):
    assert american_odds(decimal_odds(american)) == american


def test_minus_100_is_even_money():
    assert american_odds(decimal_odds(-100)) == 100


def test_implied_probability():
    assert implied_probability(-110) == pytest.approx(0.5238, abs=1e-4)
    assert implied_probability(150) == pytest.approx(0.40)


def test_no_vig_spec_example():
    fair = no_vig(implied_probability(-125), implied_probability(105))
    assert fair == pytest.approx(0.5325, abs=1e-4)
    clv_pp = (fair - implied_probability(-110)) * 100
    assert clv_pp == pytest.approx(0.87, abs=0.01)


def test_wilson_interval():
    lo, hi = wilson_interval(18, 30)
    assert (round(lo * 100, 1), round(hi * 100, 1)) == (42.3, 75.4)
    lo, hi = wilson_interval(60, 100)
    assert (round(lo * 100, 1), round(hi * 100, 1)) == (50.2, 69.1)


def test_wilson_interval_contains_break_even_in_spec_example():
    lo, hi = wilson_interval(18, 30)
    assert lo < implied_probability(-110) < hi


@pytest.mark.parametrize("bad", [99, -99, 0, 50])
def test_invalid_american_odds_rejected(bad):
    with pytest.raises(ValueError):
        decimal_odds(bad)
    with pytest.raises(ValueError):
        implied_probability(bad)


@pytest.mark.parametrize("bad", [1, "1.0", 0.5])
def test_invalid_decimal_odds_rejected(bad):
    with pytest.raises(ValueError):
        american_odds(bad)


@pytest.mark.parametrize(("wins", "n"), [(0, 0), (5, 4), (-1, 10)])
def test_wilson_invalid(wins, n):
    with pytest.raises(ValueError):
        wilson_interval(wins, n)

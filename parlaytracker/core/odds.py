"""Pure odds maths (SPEC.md section 7.3).

Anything that becomes money uses Decimal; probabilities use float.
"""
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from math import prod, sqrt

CENT = Decimal("0.01")


def _check_american(american: int) -> None:
    if -100 < american < 100:
        raise ValueError(f"American odds must be <= -100 or >= +100, got {american}")


def decimal_odds(american: int) -> Decimal:
    """+150 -> 2.5, -110 -> 1.9090..."""
    _check_american(american)
    if american > 0:
        return 1 + Decimal(american) / 100
    return 1 + Decimal(100) / -american


def american_odds(decimal: Decimal | float) -> int:
    """7.70 -> +670, 1.9091 -> -110. Rounds half away from zero."""
    d = Decimal(str(decimal))
    if d <= 1:
        raise ValueError(f"decimal odds must be greater than 1, got {decimal}")
    value = (d - 1) * 100 if d >= 2 else Decimal(-100) / (d - 1)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parlay_decimal(american: Iterable[int]) -> Decimal:
    """Decimal odds of a standard parlay: the product of its legs' decimal odds."""
    return prod((decimal_odds(a) for a in american), start=Decimal(1))


def payout(stake: Decimal, decimal: Decimal) -> Decimal:
    """Total returned, including the stake, rounded to the cent."""
    return (stake * decimal).quantize(CENT, rounding=ROUND_HALF_UP)


def implied_probability(american: int) -> float:
    """-110 -> 0.5238, +150 -> 0.40 (includes the bookmaker's margin)."""
    _check_american(american)
    if american < 0:
        return -american / (-american + 100)
    return 100 / (american + 100)


def no_vig(p_over: float, p_other: float) -> float:
    """Fair probability of one side, with the margin removed proportionally."""
    return p_over / (p_over + p_other)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a hit rate. 18 of 30 -> (0.423, 0.754)."""
    if n <= 0 or not 0 <= wins <= n:
        raise ValueError(f"need 0 <= wins <= n and n > 0, got wins={wins}, n={n}")
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half

"""Pure settlement functions (SPEC.md sections 7.1 and 7.2). No database access.

When a leg or slip settles is decided elsewhere (the worker, section 7.1); these functions
only decide the outcome from final values.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from parlaytracker.core.models import LegResult, MarketType, SlipStatus, SlipType, TeamSide
from parlaytracker.core.odds import decimal_odds, parlay_decimal, payout

# Unplaced slips are settled as if one unit had been staked (section 7.2).
UNIT_STAKE = Decimal(1)

REDUCED_PARLAY_REASON = "Reduced parlay: enter the payout from the sportsbook"


def score_value(
    market_type: MarketType, side: TeamSide | None, home_score: int, away_score: int
) -> int:
    """Final value of a market that settles on the score: total, side's score, or margin."""
    if market_type is MarketType.GAME_TOTAL:
        return home_score + away_score
    if market_type not in (MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD):
        raise ValueError(f"{market_type} does not settle on the score")
    if side is None:
        raise ValueError(f"{market_type} needs a side")
    own, opponent = (home_score, away_score) if side is TeamSide.HOME else (away_score, home_score)
    return own if market_type is MarketType.TEAM_TOTAL else own - opponent


def settle_leg(market_type: MarketType, line: Decimal, value: Decimal | int) -> LegResult:
    """WIN, LOSS or PUSH for an Over at `line`, given the final value.

    For alt spreads `value` is the chosen side's margin and `line` the spread as shown on the
    slip (e.g. -7.5): the side must win by more than -line.
    """
    if market_type is MarketType.OTHER:
        raise ValueError("'other' legs are settled manually")
    x = value + line if market_type is MarketType.ALT_SPREAD else value - line
    if x > 0:
        return LegResult.WIN
    if x == 0:
        return LegResult.PUSH
    return LegResult.LOSS


@dataclass(frozen=True)
class LegOutcome:
    result: LegResult
    american_odds: int | None


@dataclass(frozen=True)
class SlipOutcome:
    status: SlipStatus
    # Total returned, including stake (units for unplaced slips). None while pending.
    payout: Decimal | None
    # Set when a person must enter the payout; status then stays PENDING.
    review_reason: str | None = None


def settle_slip(
    slip_type: SlipType,
    is_placed: bool,
    boosted: bool,
    stake: Decimal | None,
    slip_odds: int,
    legs: Sequence[LegOutcome],
) -> SlipOutcome:
    """Apply the table in SPEC.md section 7.2. Cash-outs are a user action, not handled here."""
    if not legs:
        raise ValueError("a slip has at least one leg")
    if is_placed and stake is None:
        raise ValueError("a placed slip needs a stake")
    staked = stake if is_placed else UNIT_STAKE
    results = [leg.result for leg in legs]

    if LegResult.LOSS in results:
        return SlipOutcome(SlipStatus.LOSS, Decimal("0.00"))
    if LegResult.PENDING in results:
        return SlipOutcome(SlipStatus.PENDING, None)
    if all(r is LegResult.WIN for r in results):
        return SlipOutcome(SlipStatus.WIN, payout(staked, decimal_odds(slip_odds)))

    # From here every leg is WIN, PUSH or VOID, and at least one is PUSH or VOID.
    refund = payout(staked, Decimal(1))
    if slip_type is SlipType.SINGLE:
        status = SlipStatus.PUSH if results[0] is LegResult.PUSH else SlipStatus.VOID
        return SlipOutcome(status, refund)
    winners = [leg for leg in legs if leg.result is LegResult.WIN]
    if not winners:
        return SlipOutcome(SlipStatus.VOID, refund)
    if (
        slip_type is SlipType.PARLAY
        and not boosted
        and all(leg.american_odds is not None for leg in winners)
    ):
        reduced = parlay_decimal(leg.american_odds for leg in winners)
        return SlipOutcome(SlipStatus.WIN, payout(staked, reduced))
    # SGP or boosted: the sportsbook's own re-pricing can't be computed.
    if not is_placed:
        return SlipOutcome(SlipStatus.VOID, refund)
    return SlipOutcome(SlipStatus.PENDING, None, review_reason=REDUCED_PARLAY_REASON)


# --- An NFL player missing from ESPN's box score (SPEC.md section 7.1) -----------------------

ET = ZoneInfo("America/New_York")
NO_STAT_NO_SNAPS_REASON = "No stat line and no snaps: likely void"
PLAYED_NO_STAT_NOTE = "Played, no stat"


class MissingPlayer(StrEnum):
    WAIT = "wait"                       # nothing to go on yet
    SETTLE_FROM_NFLVERSE = "nflverse"   # nflverse has a stat line
    SETTLE_ZERO_PLAYED = "zero_played"  # no stat line, but he took snaps: value 0
    REVIEW_NO_SNAPS = "review"          # no stat line and no snaps: likely void


def tuesday_deadline(start_time: datetime) -> datetime:
    """The end of the first Tuesday (US Eastern) after the game day, in UTC.

    nflverse's stat lines and snap counts are normally out by then, so a player with neither
    after it is treated as having no snaps.
    """
    game_day = start_time.astimezone(ET).date()
    days = (1 - game_day.weekday()) % 7 or 7  # Tuesday is weekday 1
    tuesday = game_day + timedelta(days=days)
    return datetime.combine(tuesday, time.max, tzinfo=ET).astimezone(UTC)


def resolve_missing_nfl_player(
    nflverse_value: Decimal | None, offense_snaps: float | None, start_time: datetime,
    now: datetime,
) -> tuple[MissingPlayer, Decimal | None]:
    """What to do with a pending NFL player leg that ESPN's box score doesn't list.

    Never void automatically, and never assume zero without evidence (section 7.1):
    - a nflverse stat line settles it;
    - otherwise snaps > 0 mean he played: settle at 0 ("Played, no stat");
    - zero snaps, or still no snap data after the Tuesday following the game: Review;
    - otherwise wait for nflverse.
    """
    if nflverse_value is not None:
        return MissingPlayer.SETTLE_FROM_NFLVERSE, nflverse_value
    if offense_snaps is not None:
        if offense_snaps > 0:
            return MissingPlayer.SETTLE_ZERO_PLAYED, Decimal(0)
        return MissingPlayer.REVIEW_NO_SNAPS, None
    if now > tuesday_deadline(start_time):
        return MissingPlayer.REVIEW_NO_SNAPS, None
    return MissingPlayer.WAIT, None

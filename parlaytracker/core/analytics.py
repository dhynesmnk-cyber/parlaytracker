"""Analytics (SPEC.md section 10): what the logged bets say.

Everything here is a pure function over plain rows (`LegRow`, `SlipRow`), so it needs no
database to test; `load_leg_rows` and `load_slip_rows` fill the rows from Postgres.

Two views:
- **Selections** (leg level, both users pooled): is there an edge? Hit rate with a Wilson
  interval against the break-even of the odds taken, flat 1-unit ROI, and closing line value.
- **Slips** (money): what did the stakes do?

Honesty rules (section 10.3): every figure carries its sample size, a group under
`min_sample` is flagged `low_sample`, and CLV is the headline edge indicator.
"""
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from parlaytracker.core.models import Leg, LegResult, MarketType, Slip, SlipStatus, SlipType, Sport
from parlaytracker.core.odds import decimal_odds, implied_probability, no_vig, wilson_interval
from parlaytracker.core.settlement import ET

# --- Rows -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LegRow:
    """One leg with everything the selection view can group or filter by."""
    leg_id: int
    slip_id: int
    selection_key: tuple  # the same identity as LegIn.selection_key()
    logged_at: datetime
    start_time: datetime
    sport: Sport
    market: MarketType
    sportsbook: str
    slip_type: SlipType
    leg_count: int
    is_placed: bool
    logged_by: str
    line: Decimal
    odds: int | None  # None on an SGP leg without its own odds
    result: LegResult
    closing_line: Decimal | None = None
    closing_odds: int | None = None
    closing_opposite_odds: int | None = None
    tag_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class SlipRow:
    slip_id: int
    is_placed: bool
    slip_type: SlipType
    leg_count: int
    sportsbook: str
    logged_by: str
    status: SlipStatus
    stake: Decimal | None
    payout: Decimal | None
    start_time: datetime  # the earliest game on the slip
    settled_at: datetime | None


# --- Dimensions (section 10.2): columns, not tags ---------------------------------------------

ODDS_BANDS = ["≤ −150", "−149 to −111", "−110 to +100", "+101 to +150", "> +150"]
NO_ODDS = "no odds"
WINDOWS = ["before 5pm", "5–8pm", "after 8pm"]
LEAD_TIMES = ["under 1 h", "1–6 h", "6–24 h", "over 24 h", "logged after the start"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def odds_band(odds: int | None) -> str:
    if odds is None:
        return NO_ODDS
    if odds <= -150:
        return ODDS_BANDS[0]
    if odds < -110:
        return ODDS_BANDS[1]
    if odds <= 100:  # -110 to -101, and +100
        return ODDS_BANDS[2]
    if odds <= 150:
        return ODDS_BANDS[3]
    return ODDS_BANDS[4]


def start_window(start: datetime) -> str:
    hour = start.astimezone(ET).hour  # 5pm and 8pm sharp start the next window
    return WINDOWS[0] if hour < 17 else WINDOWS[1] if hour < 20 else WINDOWS[2]


def lead_time(logged_at: datetime, start: datetime) -> str:
    lead = start - logged_at
    if lead < timedelta(0):
        return LEAD_TIMES[4]
    if lead < timedelta(hours=1):
        return LEAD_TIMES[0]
    if lead < timedelta(hours=6):
        return LEAD_TIMES[1]
    if lead < timedelta(hours=24):
        return LEAD_TIMES[2]
    return LEAD_TIMES[3]


def game_month(start: datetime) -> str:
    return start.astimezone(ET).strftime("%Y-%m")


# name -> (label, key function, the order buckets appear in; None = by sample size)
LEG_DIMENSIONS: dict[str, tuple[str, Callable[[LegRow], str], list[str] | None]] = {
    "sport": ("Sport", lambda r: r.sport.value, None),
    "market": ("Market", lambda r: r.market.value, None),
    "sportsbook": ("Sportsbook", lambda r: r.sportsbook, None),
    "slip_type": ("Slip type", lambda r: r.slip_type.value, None),
    "leg_count": ("Legs on the slip", lambda r: str(r.leg_count), None),
    "placed": ("Placed or not", lambda r: "placed" if r.is_placed else "unplaced", None),
    "logged_by": ("Logged by", lambda r: r.logged_by, None),
    "odds_band": ("Odds", lambda r: odds_band(r.odds), ODDS_BANDS + [NO_ODDS]),
    "weekday": ("Weekday of the game", lambda r: WEEKDAYS[r.start_time.astimezone(ET).weekday()],
                WEEKDAYS),
    "start_window": ("Start time (Eastern)", lambda r: start_window(r.start_time), WINDOWS),
    "lead_time": ("Logged how long before the start", lambda r: lead_time(r.logged_at,
                                                                          r.start_time),
                  LEAD_TIMES),
    "month": ("Month of the game", lambda r: game_month(r.start_time), None),
}

SLIP_DIMENSIONS: dict[str, tuple[str, Callable[[SlipRow], str]]] = {
    "slip_type": ("Slip type", lambda r: r.slip_type.value),
    "leg_count": ("Legs", lambda r: str(r.leg_count)),
    "sportsbook": ("Sportsbook", lambda r: r.sportsbook),
    "month": ("Month of the game", lambda r: game_month(r.start_time)),
}


# --- Selections: hit rate, break-even, ROI, CLV -----------------------------------------------


def dedupe(rows: Iterable[LegRow]) -> list[LegRow]:
    """Identical selections count once, using the earliest record (section 10.1)."""
    first: dict[tuple, LegRow] = {}
    for row in sorted(rows, key=lambda r: (r.logged_at, r.leg_id)):
        first.setdefault(row.selection_key, row)
    return sorted(first.values(), key=lambda r: r.leg_id)


def counted(rows: Iterable[LegRow]) -> list[LegRow]:
    """The unit of the selection view: settled legs. Voids are refunded bets that never
    happened, so they are dropped; pushes are kept but are neither a win nor a loss."""
    return [r for r in rows if r.result in (LegResult.WIN, LegResult.LOSS, LegResult.PUSH)]


def price_clv(row: LegRow) -> float | None:
    """Percentage points, for a leg whose closing line is the line taken.

    The no-vig closing probability (the raw implied probability when there is no opposite
    price) minus the implied probability of the odds taken. Example: Over taken at -110
    (52.38%) closing at Over -125 / Under +105 (no-vig 53.25%) is +0.87.
    """
    if row.odds is None or row.closing_odds is None or row.closing_line != row.line:
        return None
    closing = implied_probability(row.closing_odds)
    if row.closing_opposite_odds is not None:
        closing = no_vig(closing, implied_probability(row.closing_opposite_odds))
    return (closing - implied_probability(row.odds)) * 100


def line_clv(row: LegRow) -> float | None:
    """Points the line moved in our favour, for a leg whose closing line differs.

    Totals, team totals and player props are Overs, so closing minus taken: taking Over 45.5
    that closes at 47.5 is +2.0. Alt spreads are the other way round: taken minus closing.
    """
    if row.closing_line is None or row.closing_line == row.line:
        return None
    moved = row.closing_line - row.line
    return float(-moved if row.market is MarketType.ALT_SPREAD else moved)


@dataclass(frozen=True)
class Mean:
    """A mean and the number of legs it is over: no figure without its n."""
    value: float | None
    n: int


def _mean(values: Sequence[float]) -> Mean:
    return Mean(statistics.fmean(values) if values else None, len(values))


@dataclass(frozen=True)
class SelectionStats:
    label: str
    wins: int
    losses: int
    pushes: int
    # Legs with odds: the only ones break-even, ROI and price CLV can use.
    odds_wins: int
    odds_losses: int
    break_even: float | None  # mean implied probability of the odds taken
    roi: float | None         # flat 1-unit staked on each leg with odds
    profit_units: float
    price_clv: Mean           # percentage points; legs whose closing line is the line taken
    line_clv: Mean            # points; legs whose closing line moved
    low_sample: bool

    @property
    def n(self) -> int:
        """Decided legs: the denominator of the hit rate."""
        return self.wins + self.losses

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.wins, self.n) if self.n else None

    @property
    def n_odds(self) -> int:
        return self.odds_wins + self.odds_losses

    @property
    def hit_rate_odds(self) -> float | None:
        """Hit rate over the legs that have odds: what break-even is compared with."""
        return self.odds_wins / self.n_odds if self.n_odds else None

    @property
    def edge(self) -> float | None:
        """Hit rate minus break-even over the same legs (percentage points)."""
        if self.hit_rate_odds is None or self.break_even is None:
            return None
        return (self.hit_rate_odds - self.break_even) * 100

    @property
    def interval_beats_break_even(self) -> bool | None:
        """Is the whole interval (over the legs with odds) above break-even? Anything else
        is not yet evidence of an edge (section 10.3)."""
        if not self.n_odds or self.break_even is None:
            return None
        low, _ = wilson_interval(self.odds_wins, self.n_odds)
        return low > self.break_even


def summarize(label: str, rows: Iterable[LegRow], min_sample: int) -> SelectionStats:
    """Statistics for one group of already-deduplicated, settled legs."""
    rows = list(rows)
    wins = sum(r.result is LegResult.WIN for r in rows)
    losses = sum(r.result is LegResult.LOSS for r in rows)
    with_odds = [r for r in rows if r.odds is not None and r.result is not LegResult.PUSH]
    profit = 0.0
    for r in with_odds:
        profit += float(decimal_odds(r.odds) - 1) if r.result is LegResult.WIN else -1.0
    price = [v for r in rows if (v := price_clv(r)) is not None]
    line = [v for r in rows if (v := line_clv(r)) is not None]
    return SelectionStats(
        label=label, wins=wins, losses=losses, pushes=len(rows) - wins - losses,
        odds_wins=sum(r.result is LegResult.WIN for r in with_odds),
        odds_losses=sum(r.result is LegResult.LOSS for r in with_odds),
        break_even=(statistics.fmean(implied_probability(r.odds) for r in with_odds)
                    if with_odds else None),
        roi=profit / len(with_odds) if with_odds else None,
        profit_units=profit, price_clv=_mean(price), line_clv=_mean(line),
        low_sample=wins + losses < min_sample)


def group_selections(rows: Iterable[LegRow], dimension: str | None, min_sample: int
                     ) -> list[SelectionStats]:
    """Deduplicate, keep settled legs, and summarise by `dimension` (None: one overall row).

    Buckets with a natural order (odds band, weekday, ...) come in that order, the rest by
    sample size, biggest first.
    """
    settled = counted(dedupe(rows))
    if dimension is None:
        return [summarize("All legs", settled, min_sample)]
    _, key, order = LEG_DIMENSIONS[dimension]
    buckets: dict[str, list[LegRow]] = defaultdict(list)
    for row in settled:
        buckets[key(row)].append(row)
    labels = sorted(buckets, key=(lambda b: order.index(b)) if order else
                    (lambda b: (-len(buckets[b]), b)))
    return [summarize(label, buckets[label], min_sample) for label in labels]


# --- Filters ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    """`values` maps a dimension to the buckets allowed. Tags are filtered by id: `tag_mode`
    "any" keeps legs with at least one selected tag, "all" only those with every one."""
    values: dict[str, frozenset[str]] = field(default_factory=dict)
    tag_ids: frozenset[int] = frozenset()
    tag_mode: str = "any"

    @property
    def active(self) -> int:
        """How many slices are applied: the caution of section 10.3 counts these."""
        return sum(1 for v in self.values.values() if v) + (1 if self.tag_ids else 0)


def apply_filters(rows: Iterable[LegRow], filters: Filters) -> list[LegRow]:
    kept = []
    for row in rows:
        if any(allowed and LEG_DIMENSIONS[dim][1](row) not in allowed
               for dim, allowed in filters.values.items()):
            continue
        if filters.tag_ids:
            hit = filters.tag_ids & row.tag_ids
            if not hit or (filters.tag_mode == "all" and hit != filters.tag_ids):
                continue
        kept.append(row)
    return kept


MULTIPLE_COMPARISONS = ("The more slices you look at, the more of them will look profitable "
                        "by chance. Treat a good-looking group as a question, not an answer.")


def caution(filters: Filters) -> str | None:
    """The one-line warning once more than two filters are applied (section 10.3)."""
    return MULTIPLE_COMPARISONS if filters.active > 2 else None


def filter_options(rows: Iterable[LegRow]) -> dict[str, list[str]]:
    """The buckets present in the data, for each dimension, in display order."""
    present: dict[str, set[str]] = {dim: set() for dim in LEG_DIMENSIONS}
    for row in rows:
        for dim, (_, key, _) in LEG_DIMENSIONS.items():
            present[dim].add(key(row))
    out = {}
    for dim, (_, _, order) in LEG_DIMENSIONS.items():
        out[dim] = ([b for b in order if b in present[dim]] if order
                    else sorted(present[dim]))
    return out


# --- Slips: what did the money do? ------------------------------------------------------------


@dataclass(frozen=True)
class MoneyStats:
    label: str
    n: int          # settled slips
    staked: Decimal  # units for unplaced slips
    returned: Decimal
    low_sample: bool

    @property
    def profit(self) -> Decimal:
        return self.returned - self.staked

    @property
    def roi(self) -> float | None:
        return float(self.profit / self.staked) if self.staked else None


SETTLED = {SlipStatus.WIN, SlipStatus.LOSS, SlipStatus.PUSH, SlipStatus.VOID,
           SlipStatus.CASHED_OUT}
UNIT = Decimal(1)


def _staked(row: SlipRow) -> Decimal:
    return row.stake if row.is_placed else UNIT  # "if bet at 1 unit" (section 10.1)


def money(label: str, rows: Iterable[SlipRow], min_sample: int) -> MoneyStats:
    settled = [r for r in rows if r.status in SETTLED and r.payout is not None]
    return MoneyStats(label, len(settled), sum((_staked(r) for r in settled), Decimal(0)),
                      sum((r.payout for r in settled), Decimal(0)), len(settled) < min_sample)


def group_slips(rows: Iterable[SlipRow], dimension: str | None, *, placed: bool,
                min_sample: int) -> list[MoneyStats]:
    """Placed slips in money, or unplaced slips in units, by `dimension`."""
    rows = [r for r in rows if r.is_placed is placed]
    if dimension is None:
        return [money("All slips", rows, min_sample)]
    _, key = SLIP_DIMENSIONS[dimension]
    buckets: dict[str, list[SlipRow]] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    if dimension in ("month", "leg_count"):
        ordered = sorted(buckets, key=int if dimension == "leg_count" else str)
    else:
        ordered = sorted(buckets, key=lambda b: (-len(buckets[b]), b))
    return [money(label, buckets[label], min_sample) for label in ordered]


def cumulative_profit(rows: Iterable[SlipRow], *, placed: bool = True
                      ) -> list[tuple[datetime, Decimal]]:
    """Running profit in the order slips settled: the data for the cumulative P/L chart."""
    settled = sorted((r for r in rows if r.is_placed is placed and r.status in SETTLED
                      and r.payout is not None and r.settled_at is not None),
                     key=lambda r: (r.settled_at, r.slip_id))
    total = Decimal(0)
    series = []
    for r in settled:
        total += r.payout - _staked(r)
        series.append((r.settled_at, total))
    return series


# --- Loading ----------------------------------------------------------------------------------


def load_leg_rows(session: Session) -> list[LegRow]:
    """Every non-`other` leg with a line, ready for `group_selections`."""
    legs = session.scalars(
        select(Leg).where(Leg.market_type != MarketType.OTHER)
        .options(selectinload(Leg.slip).selectinload(Slip.legs),
                 selectinload(Leg.slip).selectinload(Slip.sportsbook),
                 selectinload(Leg.event), selectinload(Leg.tags))
        .order_by(Leg.id)).all()
    return [LegRow(
        leg_id=leg.id, slip_id=leg.slip_id,
        selection_key=(leg.event_id, leg.market_type, leg.side, leg.espn_athlete_id, leg.line,
                       leg.description),
        logged_at=leg.slip.created_at, start_time=leg.event.start_time, sport=leg.event.sport,
        market=leg.market_type, sportsbook=leg.slip.sportsbook.name,
        slip_type=leg.slip.slip_type, leg_count=len(leg.slip.legs),
        is_placed=leg.slip.is_placed, logged_by=leg.slip.logged_by, line=leg.line,
        odds=leg.american_odds, result=leg.result, closing_line=leg.closing_line,
        closing_odds=leg.closing_odds, closing_opposite_odds=leg.closing_opposite_odds,
        tag_ids=frozenset(t.id for t in leg.tags)) for leg in legs]


def load_slip_rows(session: Session) -> list[SlipRow]:
    slips = session.scalars(
        select(Slip).options(selectinload(Slip.legs).selectinload(Leg.event),
                             selectinload(Slip.sportsbook)).order_by(Slip.id)).all()
    return [SlipRow(
        slip_id=s.id, is_placed=s.is_placed, slip_type=s.slip_type, leg_count=len(s.legs),
        sportsbook=s.sportsbook.name, logged_by=s.logged_by, status=s.status, stake=s.stake,
        payout=s.payout, start_time=min(leg.event.start_time for leg in s.legs),
        settled_at=s.settled_at) for s in slips]

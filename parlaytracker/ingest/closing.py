"""Choose a closing line from parsed Odds API data (SPEC.md section 8.2, step 5). Pure: no I/O.

For each leg, the first of these that exists wins:
  1. the exact line at the leg's sportsbook, in the main market;
  2. the exact line at the leg's sportsbook, in the alternate market;
  3. the main line at the leg's sportsbook (line CLV only);
  4. the median main line across US books.
"""
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from parlaytracker.core.models import MarketType, TeamSide
from parlaytracker.core.odds import american_odds, implied_probability
from parlaytracker.ingest.odds_api import ApiMarket, ApiOutcome, EventOdds
from parlaytracker.ingest.resolve import MARKET_KEYS, same_player, same_team


class Basis(StrEnum):
    EXACT_MAIN = "exact_main"
    EXACT_ALTERNATE = "exact_alternate"
    BOOK_MAIN = "book_main"
    MEDIAN_MAIN = "median_main"

    @property
    def exact(self) -> bool:
        """Same line as the bet, so the price comparison is a true CLV."""
        return self in (Basis.EXACT_MAIN, Basis.EXACT_ALTERNATE)


@dataclass(frozen=True)
class LegQuery:
    """The parts of a leg that matter for matching."""
    market_type: MarketType
    line: Decimal
    side: TeamSide | None
    player_name: str | None
    book_key: str | None  # the leg's sportsbook in Odds API terms; None if it has none


@dataclass(frozen=True)
class Quote:
    point: Decimal
    price: int  # the Over side, or the chosen team for a spread
    opposite_price: int | None  # the Under, or the other team


@dataclass(frozen=True)
class Closing:
    line: Decimal
    odds: int
    opposite_odds: int | None
    basis: Basis


def _valid(price: int) -> bool:
    return price >= 100 or price <= -100


def _point(outcome: ApiOutcome) -> Decimal | None:
    # Through str: float 71.5 -> Decimal("71.5"), not a binary expansion.
    return None if outcome.point is None else Decimal(str(outcome.point))


def _teams(leg: LegQuery, event: EventOdds) -> tuple[str, str]:
    """(the leg's team, the other team)."""
    home, away = event.home_team, event.away_team
    return (home, away) if leg.side is TeamSide.HOME else (away, home)


def quotes(leg: LegQuery, market: ApiMarket, event: EventOdds) -> list[Quote]:
    """Every price in `market` that could be this leg's bet, at any line."""
    outcomes = [o for o in market.outcomes if _valid(o.price) and o.point is not None]
    if leg.market_type is MarketType.ALT_SPREAD:
        team, other = _teams(leg, event)
        mine = [o for o in outcomes if same_team(o.name, team)]
        theirs = {_point(o): o.price for o in outcomes if same_team(o.name, other)}
        return [Quote(_point(o), o.price, theirs.get(-_point(o))) for o in mine]  # type: ignore[arg-type]

    if leg.market_type is MarketType.GAME_TOTAL:
        subject = outcomes
    elif leg.market_type is MarketType.TEAM_TOTAL:
        team, _ = _teams(leg, event)
        subject = [o for o in outcomes if o.description and same_team(o.description, team)]
    else:  # player markets
        subject = [o for o in outcomes if o.description and leg.player_name
                   and same_player(leg.player_name, o.description)]
    unders = {(o.description, _point(o)): o.price for o in subject if o.name == "Under"}
    return [Quote(_point(o), o.price, unders.get((o.description, _point(o))))
            for o in subject if o.name == "Over"]


def _book_markets(event: EventOdds, book_key: str, market_key: str) -> list[ApiMarket]:
    return [m for b in event.bookmakers if b.key == book_key
            for m in b.markets if m.key == market_key]


def book_quotes(leg: LegQuery, event: EventOdds, book_key: str, market_key: str) -> list[Quote]:
    return [q for m in _book_markets(event, book_key, market_key)
            for q in quotes(leg, m, event)]


def _exact(leg: LegQuery, found: Iterable[Quote], basis: Basis) -> Closing | None:
    for q in found:
        if q.point == leg.line:
            return Closing(q.point, q.price, q.opposite_price, basis)
    return None


def find_exact_main(leg: LegQuery, event: EventOdds) -> Closing | None:
    if leg.book_key is None:
        return None
    keys = MARKET_KEYS[leg.market_type]
    return _exact(leg, book_quotes(leg, event, leg.book_key, keys.main), Basis.EXACT_MAIN)


def find_exact_alternate(leg: LegQuery, event: EventOdds) -> Closing | None:
    """`event` is the follow-up response, which holds only alternate markets."""
    if leg.book_key is None:
        return None
    keys = MARKET_KEYS[leg.market_type]
    return _exact(leg, book_quotes(leg, event, leg.book_key, keys.alternate),
                  Basis.EXACT_ALTERNATE)


def find_book_main(leg: LegQuery, event: EventOdds) -> Closing | None:
    if leg.book_key is None:
        return None
    found = book_quotes(leg, event, leg.book_key, MARKET_KEYS[leg.market_type].main)
    return Closing(found[0].point, found[0].price, found[0].opposite_price,
                   Basis.BOOK_MAIN) if found else None


def _median_odds(prices: list[int]) -> int:
    """Median of the implied probabilities, converted back: averaging American odds across
    the +100/-100 gap would be meaningless."""
    p = statistics.median(implied_probability(x) for x in prices)
    return american_odds(Decimal(1) / Decimal(str(p)))


def find_median_main(leg: LegQuery, event: EventOdds) -> Closing | None:
    main = MARKET_KEYS[leg.market_type].main
    per_book = [q for b in event.bookmakers for q in book_quotes(leg, event, b.key, main)[:1]]
    if not per_book:
        return None
    line = statistics.median_low(q.point for q in per_book)  # a real line, never x.25
    at_line = [q for q in per_book if q.point == line]
    opposite = [q.opposite_price for q in at_line if q.opposite_price is not None]
    return Closing(line, _median_odds([q.price for q in at_line]),
                   _median_odds(opposite) if opposite else None, Basis.MEDIAN_MAIN)


def needs_alternate_call(leg: LegQuery, event: EventOdds) -> bool:
    """Only worth a follow-up call if the leg's book is in the response at all."""
    return leg.book_key is not None and any(b.key == leg.book_key for b in event.bookmakers)

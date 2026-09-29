"""Which markets each sport supports (SPEC.md section 1.2)."""
from parlaytracker.core.models import MarketType, Sport

_ALL = frozenset(Sport)

MARKET_SPORTS: dict[MarketType, frozenset[Sport]] = {
    MarketType.GAME_TOTAL: _ALL,
    MarketType.TEAM_TOTAL: _ALL,
    MarketType.ALT_SPREAD: _ALL,
    MarketType.PLAYER_RECEPTIONS: frozenset({Sport.NFL}),
    MarketType.PLAYER_RECEIVING_YARDS: frozenset({Sport.NFL}),
    MarketType.PLAYER_RUSHING_YARDS: frozenset({Sport.NFL}),
    MarketType.PLAYER_PASSING_YARDS: frozenset({Sport.NFL}),
    MarketType.PLAYER_PASS_COMPLETIONS: frozenset({Sport.NFL}),
    MarketType.PLAYER_TOUCHDOWNS: frozenset({Sport.NFL}),
    MarketType.PLAYER_INTERCEPTIONS: frozenset({Sport.NFL}),
    MarketType.PLAYER_FIELD_GOALS: frozenset({Sport.NFL}),
    MarketType.PLAYER_POINTS: frozenset({Sport.NBA, Sport.NHL}),
    MarketType.OTHER: _ALL,
}


def markets_for(sport: Sport) -> list[MarketType]:
    return [m for m, sports in MARKET_SPORTS.items() if sport in sports]


# Markets the worker cannot fetch a closing line for: The Odds API's keys for these are not yet
# verified against a real game, so asking for one could get a whole request rejected. They can
# still be entered by hand, and the Review queue does not nag about them.
NO_AUTO_CLOSING: frozenset[MarketType] = frozenset({
    MarketType.OTHER, MarketType.PLAYER_PASS_COMPLETIONS, MarketType.PLAYER_TOUCHDOWNS,
    MarketType.PLAYER_INTERCEPTIONS, MarketType.PLAYER_FIELD_GOALS,
})

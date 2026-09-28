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
    MarketType.PLAYER_POINTS: frozenset({Sport.NBA, Sport.NHL}),
    MarketType.OTHER: _ALL,
}


def markets_for(sport: Sport) -> list[MarketType]:
    return [m for m, sports in MARKET_SPORTS.items() if sport in sports]

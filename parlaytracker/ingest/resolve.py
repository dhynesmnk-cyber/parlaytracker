"""Alias tables and matching between ParlayTracker and The Odds API (SPEC.md section 6.2).

Phase 3 covers what closing-line capture needs: market keys, sport keys, team names and player
names. Later phases add sportsbook and market wording for screenshot extraction.
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from parlaytracker.core.models import MarketType, Sport

SPORT_KEYS: dict[Sport, str] = {
    Sport.NFL: "americanfootball_nfl",
    Sport.NBA: "basketball_nba",
    Sport.MLB: "baseball_mlb",
    Sport.NHL: "icehockey_nhl",
}


@dataclass(frozen=True)
class MarketKeys:
    main: str
    alternate: str


# One dict, per section 6.2. NFL keys were verified against a real game; `player_points` is
# still to verify on an NBA or NHL game.
MARKET_KEYS: dict[MarketType, MarketKeys] = {
    MarketType.GAME_TOTAL: MarketKeys("totals", "alternate_totals"),
    MarketType.TEAM_TOTAL: MarketKeys("team_totals", "alternate_team_totals"),
    MarketType.ALT_SPREAD: MarketKeys("spreads", "alternate_spreads"),
    MarketType.PLAYER_RECEPTIONS: MarketKeys("player_receptions", "player_receptions_alternate"),
    MarketType.PLAYER_RECEIVING_YARDS: MarketKeys("player_reception_yds",
                                                  "player_reception_yds_alternate"),
    MarketType.PLAYER_RUSHING_YARDS: MarketKeys("player_rush_yds", "player_rush_yds_alternate"),
    MarketType.PLAYER_PASSING_YARDS: MarketKeys("player_pass_yds", "player_pass_yds_alternate"),
    MarketType.PLAYER_POINTS: MarketKeys("player_points", "player_points_alternate"),
}

# The Odds API's team names against ESPN's displayName. They match today (verified for NFL);
# record any team that differs here, in normalised form.
TEAM_ALIASES: dict[str, str] = {}

EVENT_TIME_TOLERANCE = timedelta(hours=3)
PLAYER_MATCH_MIN_SCORE = 90


def normalize_name(name: str) -> str:
    """Lowercase, ASCII, no punctuation: 'D'Andre Swift' -> 'dandre swift'."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("-", " ")).strip()


def _team_key(name: str) -> str:
    key = normalize_name(name)
    return TEAM_ALIASES.get(key, key)


def same_team(a: str, b: str) -> bool:
    return _team_key(a) == _team_key(b)


def same_game(home: str, away: str, start: datetime, api_home: str, api_away: str,
              api_start: datetime) -> bool:
    """Both teams match, and the start times are within 3 hours."""
    return (same_team(home, api_home) and same_team(away, api_away)
            and abs(start - api_start) <= EVENT_TIME_TOLERANCE)


_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def _player_key(name: str) -> str:
    return re.sub(r"\s+", " ", _SUFFIXES.sub("", normalize_name(name))).strip()


def same_player(leg_name: str, api_name: str) -> bool:
    """Fuzzy match, a score of at least 90 (section 6.2). Suffixes such as 'Jr.' are ignored."""
    score = fuzz.token_sort_ratio(_player_key(leg_name), _player_key(api_name))
    return score >= PLAYER_MATCH_MIN_SCORE


"""Alias tables and matching (SPEC.md sections 6.2 and 9.6).

The first half is what closing-line capture needs to speak to The Odds API: market keys, sport
keys, team and player names. The second half resolves what the model read from a slip
screenshot into what the form pre-fills.
"""
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from rapidfuzz import fuzz

from parlaytracker.core.markets import markets_for
from parlaytracker.core.models import MarketType, Sport, TeamSide
from parlaytracker.core.schemas import ExtractedLeg, ExtractedSlip
from parlaytracker.ingest import espn
from parlaytracker.ingest.http import FetchError, RateLimited

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



# =============================================================================================
# Screenshot resolution (SPEC.md section 9.6): what the model read -> what the form pre-fills.
# Deterministic, no model call. Anything ambiguous is `doubtful`, never silently chosen.
# =============================================================================================

PLAYER_AUTO_SCORE = 90    # select automatically
PLAYER_DOUBTFUL_SCORE = 75  # 75-89: pre-select, but highlight; below: leave blank
AMBIGUOUS_MARGIN = 3      # a runner-up this close makes even a good match doubtful

# --- Sport ---------------------------------------------------------------------------------

SPORT_WORDS: dict[str, Sport] = {
    "nfl": Sport.NFL, "football": Sport.NFL, "nba": Sport.NBA, "basketball": Sport.NBA,
    "mlb": Sport.MLB, "baseball": Sport.MLB, "nhl": Sport.NHL, "hockey": Sport.NHL,
}


def resolve_sport(text: str | None) -> Sport | None:
    tokens = set(normalize_name(text or "").split())
    found = {sport for word, sport in SPORT_WORDS.items() if word in tokens}
    return found.pop() if len(found) == 1 else None


# --- Market --------------------------------------------------------------------------------

# Checked in order: the first pattern that matches wins, so specific wording comes first.
_MARKET_PATTERNS: list[tuple[re.Pattern[str], MarketType]] = [(re.compile(p), m) for p, m in [
    (r"\bteam (total|points)\b", MarketType.TEAM_TOTAL),
    (r"\brec(eiving|eptions?)? ?(yd|yds|yards)\b|\breceiving\b", MarketType.PLAYER_RECEIVING_YARDS),
    (r"\breceptions?\b|\brecs?\b", MarketType.PLAYER_RECEPTIONS),
    (r"\brush(ing)? ?(yd|yds|yards)\b|\brushing\b", MarketType.PLAYER_RUSHING_YARDS),
    (r"\bpass(ing)? ?(yd|yds|yards)\b|\bpassing\b", MarketType.PLAYER_PASSING_YARDS),
    (r"\b(alt(ernate)?|point|run|puck) ?(spread|line)\b|\bspread\b", MarketType.ALT_SPREAD),
    (r"\b(game total|total points|total|over under|o u)\b", MarketType.GAME_TOTAL),
    (r"\bpoints?\b|\bpts\b", MarketType.PLAYER_POINTS),
]]
_UNDER = re.compile(r"\bunder\b|\bu ?\d")
_OVER_UNDER = re.compile(r"\bover under\b|\bo u\b")
_SPORT_BY_MARKET_WORD = {"puck line": Sport.NHL, "run line": Sport.MLB}


@dataclass(frozen=True)
class ResolvedMarket:
    market: MarketType
    doubtful: bool = False


def resolve_market(text: str | None, sport: Sport, has_player: bool) -> ResolvedMarket:
    """A market name as printed -> a MarketType. An Under, or wording we don't support, is an
    `other` leg (settled by hand and not analysed), never a guess."""
    norm = normalize_name((text or "").replace("/", " "))
    if not norm:
        return ResolvedMarket(MarketType.OTHER, doubtful=True)
    norm = _OVER_UNDER.sub("total", norm)  # "Over/Under" names the market, it isn't an Under
    if _UNDER.search(norm):
        return ResolvedMarket(MarketType.OTHER)
    for pattern, market in _MARKET_PATTERNS:
        if pattern.search(norm):
            if market is MarketType.PLAYER_POINTS and not has_player:
                continue  # "points" with no player is not a player prop
            if market not in markets_for(sport):
                return ResolvedMarket(MarketType.OTHER)
            return ResolvedMarket(market)
    return ResolvedMarket(MarketType.OTHER)


def market_sport_hint(text: str | None) -> Sport | None:
    norm = normalize_name(text or "")
    for phrase, sport in _SPORT_BY_MARKET_WORD.items():
        if phrase in norm:
            return sport
    if re.search(r"\brec(eiving|eptions?)?\b|\brush(ing)?\b|\bpass(ing)? (yd|yds|yards)\b", norm):
        return Sport.NFL
    return None


# --- Sportsbook ----------------------------------------------------------------------------

# Wording printed on slips -> the name the sportsbook is stored under (normalised).
BOOK_ALIASES: dict[str, str] = {
    "dk": "draftkings", "draft kings": "draftkings", "dkng": "draftkings",
    "draftkings sportsbook": "draftkings",
    "fd": "fanduel", "fan duel": "fanduel", "fanduel sportsbook": "fanduel",
    "mgm": "betmgm", "bet mgm": "betmgm",
    "caesars sportsbook": "caesars", "williamhill": "caesars", "william hill": "caesars",
    "espnbet": "espn bet", "penn": "espn bet",
}


def resolve_sportsbook(text: str | None, books: dict[int, str]) -> int | None:
    """The stored sportsbook a printed name means, or None (the form then asks)."""
    norm = normalize_name(text or "")
    if not norm:
        return None
    norm = BOOK_ALIASES.get(norm, norm)
    by_name = {normalize_name(name): book_id for book_id, name in books.items()}
    if norm in by_name:
        return by_name[norm]
    for name, book_id in by_name.items():  # "draftkings" inside "draftkings sportsbook ny"
        if name and re.search(rf"\b{re.escape(name)}\b", norm):
            return book_id
    hit = max(((fuzz.token_set_ratio(norm, name), name) for name in by_name), default=(0, ""))
    return by_name[hit[1]] if hit[0] >= PLAYER_AUTO_SCORE else None


# --- Teams and events ----------------------------------------------------------------------


def team_aliases(display_name: str, abbreviation: str) -> frozenset[str]:
    """Everything a slip might call a team: "Chicago Bears" -> chicago bears, chicago, bears, chi.

    Location and nickname are the leading and trailing words, so "Boston Red Sox" gives
    "boston", "red sox" and "sox" too.
    """
    words = normalize_name(display_name).split()
    aliases = {" ".join(words), normalize_name(abbreviation)}
    for n in range(1, len(words)):
        location, nickname = " ".join(words[:n]), " ".join(words[n:])
        if location not in GENERIC_WORDS:  # "new" and "los" name half the league
            aliases.add(location)
        aliases.add(nickname)
    return frozenset(a for a in aliases if a)


# First words that many teams share: never an alias by themselves.
GENERIC_WORDS = frozenset({"new", "los", "san", "las", "st", "saint", "golden", "north", "south",
                           "west", "east", "tampa"})


def _mentions(text_norm: str, aliases: frozenset[str]) -> bool:
    padded = f" {text_norm} "
    return any(f" {a} " in padded for a in aliases)


def _game_aliases(game: espn.Game) -> tuple[frozenset[str], frozenset[str]]:
    """Each team's aliases, minus any the two teams share ("New York" in Jets @ Giants):
    a word that could mean either team identifies neither."""
    home = team_aliases(game.home.name, game.home.abbreviation)
    away = team_aliases(game.away.name, game.away.abbreviation)
    return home - away, away - home


@dataclass(frozen=True)
class GameMatch:
    game: espn.Game | None
    doubtful: bool = False


def match_game(event_text: str | None, team_text: str | None,
               games: list[espn.Game]) -> GameMatch:
    """The ESPN game a slip's event wording means, among `games` (the slip's day, give or
    take a day). Both teams named: a match (doubtful only if more than one game fits, such as
    a doubleheader). One team named: a match only if exactly one game has it, and doubtful."""
    text = normalize_name(f"{event_text or ''} {team_text or ''}")
    if not text:
        return GameMatch(None)
    both, one = [], []
    for game in games:
        home, away = _game_aliases(game)
        hits = _mentions(text, home) + _mentions(text, away)
        (both if hits == 2 else one if hits == 1 else []).append(game)
    if len(both) == 1:
        return GameMatch(both[0])
    if both:
        return GameMatch(sorted(both, key=lambda g: g.start_time)[0], doubtful=True)
    if len(one) == 1:
        return GameMatch(one[0], doubtful=True)
    return GameMatch(None)


def match_side(team_text: str | None, game: espn.Game | None) -> TeamSide | None:
    """Home or away, from the team's name; None when neither (or both) is named."""
    if game is None or not team_text:
        return None
    text = normalize_name(team_text)
    home, away = _game_aliases(game)
    is_home, is_away = _mentions(text, home), _mentions(text, away)
    if is_home == is_away:
        return None
    return TeamSide.HOME if is_home else TeamSide.AWAY


# --- Players -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayerMatch:
    athlete_id: str | None
    name: str | None = None
    score: float = 0.0
    doubtful: bool = False


def match_roster_player(name: str | None, roster: list[espn.RosterPlayer]) -> PlayerMatch:
    """Match a printed name against the event's two rosters (section 9.6): 90 or above selects
    automatically, 75-89 pre-selects but is highlighted, below 75 leaves it blank. A runner-up
    within a few points (two players named alike) also makes the match doubtful."""
    if not name or not roster:
        return PlayerMatch(None)
    key = _player_key(name)
    scored = sorted(((fuzz.token_sort_ratio(key, _player_key(p.name)), p) for p in roster),
                    key=lambda sp: (-sp[0], sp[1].unavailable, sp[1].name))
    best_score, best = scored[0]
    if best_score < PLAYER_DOUBTFUL_SCORE:
        return PlayerMatch(None)
    runner_up = next((s for s, p in scored[1:] if p.espn_athlete_id != best.espn_athlete_id), 0)
    doubtful = best_score < PLAYER_AUTO_SCORE or best_score - runner_up < AMBIGUOUS_MARGIN
    return PlayerMatch(best.espn_athlete_id, best.name, best_score, doubtful)


# --- Putting it together ---------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLeg:
    sport: Sport
    day: date
    game: espn.Game | None
    market: MarketType
    athlete_id: str | None = None
    side: TeamSide | None = None
    description: str | None = None
    line: Decimal | None = None
    odds: int | None = None
    doubts: frozenset[str] = frozenset()  # "sport", "game", "market", "player", "side", ...


@dataclass(frozen=True)
class ResolvedSlip:
    legs: list[ResolvedLeg]
    book_id: int | None
    slip_odds: int | None
    stake: Decimal | None
    payout: Decimal | None
    placed: bool
    sgp: bool | None  # what the slip says, when it says; None: let the form infer it
    doubts: frozenset[str] = frozenset()  # "book", "odds", "stake"


def _odds(value: int | None) -> int | None:
    return value if value is not None and (value <= -100 or value >= 100) else None


def _money(value: float | None) -> Decimal | None:
    return None if value is None or value <= 0 else Decimal(str(value)).quantize(Decimal("0.01"))


def _line(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _describe(leg: ExtractedLeg) -> str:
    """An `other` leg keeps the slip's own wording as its description (section 9.6)."""
    parts = [leg.player_name, leg.team_text, leg.market_text,
             None if leg.line is None else f"{leg.line:g}"]
    return " ".join(p for p in parts if p) or "Unreadable selection"


def resolve_slip(
    extracted: ExtractedSlip, *, books: dict[int, str], day: date, default_sport: Sport,
    games_for: Callable[[Sport, date], list[espn.Game]],
    roster_for: Callable[[Sport, str], list[espn.RosterPlayer]],
) -> ResolvedSlip:
    """What the form should be pre-filled with. `games_for` and `roster_for` may raise
    FetchError or RateLimited: the affected field is then left blank and highlighted."""
    def games_near(sport: Sport) -> list[espn.Game]:
        found: list[espn.Game] = []
        for offset in (0, -1, 1):  # the slip's day first, then give or take a day
            try:
                found += games_for(sport, day + timedelta(days=offset))
            except (FetchError, RateLimited):
                continue
        return found

    legs: list[ResolvedLeg] = []
    for leg in extracted.legs:
        doubts: set[str] = set()
        sport = (resolve_sport(leg.sport) or resolve_sport(leg.event_text)
                 or market_sport_hint(leg.market_text))
        if sport is None:
            sport = default_sport
            doubts.add("sport")
        match = match_game(leg.event_text, leg.team_text, games_near(sport))
        if match.game is None or match.doubtful:
            doubts.add("game")
        resolved = resolve_market(leg.market_text, sport, has_player=bool(leg.player_name))
        if resolved.doubtful:
            doubts.add("market")
        market = resolved.market
        athlete_id = side = description = line = None
        if market is MarketType.OTHER:
            description = _describe(leg)
        else:
            line = _line(leg.line)
            if line is None or (line * 2) % 1 != 0:
                doubts.add("line")
            if market.value.startswith("player_"):
                athlete_id = _match_player(leg, match.game, sport, roster_for, doubts)
            elif market in (MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD):
                side = match_side(leg.team_text, match.game)
                if side is None:
                    doubts.add("side")
        odds = _odds(leg.american_odds)
        if leg.american_odds is not None and odds is None:
            doubts.add("odds")
        game_day = espn.game_day(match.game.start_time) if match.game else day
        legs.append(ResolvedLeg(sport, game_day, match.game, market, athlete_id, side,
                                description, line, odds, frozenset(doubts)))

    slip_odds = _odds(extracted.american_odds)
    if len(legs) == 1 and legs[0].odds is None and slip_odds is not None:
        legs[0] = ResolvedLeg(**{**legs[0].__dict__, "odds": slip_odds})  # a single's odds
    book_id = resolve_sportsbook(extracted.sportsbook_text, books)
    slip_doubts = set()
    if book_id is None:
        slip_doubts.add("book")
    stake, payout = _money(extracted.stake), _money(extracted.potential_payout)
    kind = normalize_name(extracted.slip_type_text or "")
    sgp = True if re.search(r"\bsgp\b|same game", kind) else False if "parlay" in kind else None
    return ResolvedSlip(legs, book_id, slip_odds, stake, payout, placed=stake is not None,
                        sgp=sgp, doubts=frozenset(slip_doubts))


def _match_player(leg: ExtractedLeg, game: espn.Game | None, sport: Sport,
                  roster_for: Callable[[Sport, str], list[espn.RosterPlayer]],
                  doubts: set[str]) -> str | None:
    if game is None or not leg.player_name:
        doubts.add("player")
        return None
    roster: list[espn.RosterPlayer] = []
    for team in (game.away, game.home):
        try:
            roster += roster_for(sport, team.espn_id)
        except (FetchError, RateLimited):
            continue
    found = match_roster_player(leg.player_name, roster)
    if found.athlete_id is None or found.doubtful:
        doubts.add("player")
    return found.athlete_id

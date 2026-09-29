"""ESPN client and parsers (SPEC.md section 6.1).

Nothing outside this module touches ESPN's raw JSON: parsers validate the parts they use
with Pydantic and return typed dataclasses. A malformed event is reported for that event
only; a malformed document raises SchemaError.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from parlaytracker.core.models import DataSource, EventStatus, FailureKind, MarketType, Sport
from parlaytracker.ingest.http import FetchError, RateLimiter

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
BASE_PATH = "/apis/site/v2/sports"
SPORT_PATHS = {
    Sport.NFL: "football/nfl",
    Sport.NBA: "basketball/nba",
    Sport.MLB: "baseball/mlb",
    Sport.NHL: "hockey/nhl",
}
# Tried in order (section 6.1). cdn.espn.com serves the box score only, wrapped in
# `gamepackageJSON`; see CDN_URL and `parse_box_score`.
HOSTS: list[tuple[DataSource, str]] = [
    (DataSource.ESPN_WEB, "https://site.web.api.espn.com"),
    (DataSource.ESPN_SITE, "https://site.api.espn.com"),
]

CDN_URL = "https://cdn.espn.com/core/{league}/game"
CDN_LEAGUES = {Sport.NFL: "nfl", Sport.NBA: "nba", Sport.MLB: "mlb", Sport.NHL: "nhl"}

# One limiter per process for all ESPN hosts: 1 request / 2 s per host, 20 / minute overall.
LIMITER = RateLimiter(per_host_interval=2.0, per_minute=20)


class SchemaError(Exception):
    """ESPN's document doesn't have the shape the parser expects (section 8.3: `schema`)."""


def game_day(moment: datetime) -> date:
    """ESPN files games under their US Eastern calendar date (verified, section 3)."""
    return moment.astimezone(ET).date()


def today_game_day(now: datetime | None = None) -> date:
    return game_day(now or datetime.now(tz=ET))


# --- Parsed results ------------------------------------------------------------------------


@dataclass(frozen=True)
class Team:
    espn_id: str
    abbreviation: str
    name: str


@dataclass(frozen=True)
class Game:
    espn_event_id: str
    sport: Sport
    start_time: datetime  # UTC
    status: EventStatus
    status_detail: str
    period: int | None
    clock_seconds: int | None
    home: Team
    away: Team
    home_score: int | None
    away_score: int | None

    @property
    def label(self) -> str:
        return f"{self.away.abbreviation} @ {self.home.abbreviation}"


@dataclass(frozen=True)
class ScoreboardResult:
    games: list[Game]
    errors: dict[str, str]  # event id (or position) -> why it couldn't be parsed


@dataclass(frozen=True)
class RosterPlayer:
    espn_athlete_id: str
    name: str
    position: str
    group: str  # e.g. "offense", "injuredReserveOrOut"; "" when ESPN doesn't group
    unavailable: bool  # injured, suspended or otherwise not active

    @property
    def label(self) -> str:
        return f"{self.name} ({self.position})" if self.position else self.name


@dataclass(frozen=True)
class BoxScore:
    """What settlement needs from a summary (or cdn) document."""
    espn_event_id: str
    status: EventStatus
    home_espn_team_id: str
    away_espn_team_id: str
    home_score: int | None
    away_score: int | None
    # market -> ESPN athlete id -> final value, for the player markets the sport has
    stats: dict[MarketType, dict[str, Decimal]]
    did_not_play: frozenset[str]  # athletes ESPN says didn't play (NBA `didNotPlay`)


# --- Raw shapes (only the fields we use) ---------------------------------------------------


class _TeamRaw(BaseModel):
    id: str
    abbreviation: str
    displayName: str


class _CompetitorRaw(BaseModel):
    homeAway: Literal["home", "away"]
    score: str | None = None
    team: _TeamRaw


class _CompetitionRaw(BaseModel):
    competitors: list[_CompetitorRaw]


class _StatusTypeRaw(BaseModel):
    name: str
    state: Literal["pre", "in", "post"]
    completed: bool = False
    detail: str = ""


class _StatusRaw(BaseModel):
    type: _StatusTypeRaw
    period: int | None = None
    clock: float | None = None


class _EventRaw(BaseModel):
    id: str
    date: datetime
    status: _StatusRaw
    competitions: list[_CompetitionRaw]


class _PositionRaw(BaseModel):
    abbreviation: str = ""


class _AthleteStatusRaw(BaseModel):
    type: str = ""
    name: str = ""


class _AthleteRaw(BaseModel):
    id: str
    fullName: str | None = None
    displayName: str | None = None
    position: _PositionRaw | None = None
    status: _AthleteStatusRaw | None = None


# --- Status mapping ------------------------------------------------------------------------

_BREAK_NAMES = {"STATUS_HALFTIME", "STATUS_END_PERIOD", "STATUS_END_OF_PERIOD"}
_warned_names: set[str] = set()


def map_status(name: str, state: str, completed: bool) -> EventStatus:
    """ESPN status -> EventStatus, by name first and state second (section 6.1).

    A postponed game has state "post" too, so "post" alone never means final.
    """
    if name == "STATUS_POSTPONED":
        return EventStatus.POSTPONED
    if name in ("STATUS_CANCELED", "STATUS_CANCELLED"):
        return EventStatus.CANCELLED
    if name == "STATUS_FINAL" or (state == "post" and completed):
        return EventStatus.FINAL
    if name in _BREAK_NAMES:
        return EventStatus.BREAK
    if "DELAY" in name or "SUSPENDED" in name:
        return EventStatus.DELAYED
    if name not in ("STATUS_SCHEDULED", "STATUS_IN_PROGRESS") and name not in _warned_names:
        _warned_names.add(name)
        log.warning("unrecognised ESPN status %s (state %s); mapping by state", name, state)
    if state == "in":
        return EventStatus.IN_PROGRESS
    if state == "pre":
        return EventStatus.SCHEDULED
    return EventStatus.POSTPONED  # post but not completed: not played to a finish


# --- Parsers -------------------------------------------------------------------------------


def _score(raw: str | None, state: str) -> int | None:
    if state == "pre" or raw is None or raw == "":
        return None
    return int(raw)


def _parse_event(sport: Sport, raw: Any) -> Game:
    event = _EventRaw.model_validate(raw)
    if len(event.competitions) != 1:
        raise ValueError(f"expected 1 competition, got {len(event.competitions)}")
    sides = {c.homeAway: c for c in event.competitions[0].competitors}
    if set(sides) != {"home", "away"}:
        raise ValueError("expected one home and one away competitor")
    st = event.status
    teams = {k: Team(v.team.id, v.team.abbreviation, v.team.displayName) for k, v in sides.items()}
    return Game(
        espn_event_id=event.id,
        sport=sport,
        start_time=event.date,
        status=map_status(st.type.name, st.type.state, st.type.completed),
        status_detail=st.type.detail,
        period=st.period,
        clock_seconds=None if st.clock is None else int(st.clock),
        home=teams["home"],
        away=teams["away"],
        home_score=_score(sides["home"].score, st.type.state),
        away_score=_score(sides["away"].score, st.type.state),
    )


def parse_scoreboard(sport: Sport, payload: Any) -> ScoreboardResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise SchemaError("scoreboard has no 'events' list")
    games, errors = [], {}
    for n, raw in enumerate(payload["events"]):
        key = str(raw.get("id", f"#{n}")) if isinstance(raw, dict) else f"#{n}"
        try:
            games.append(_parse_event(sport, raw))
        except (ValidationError, ValueError) as e:
            errors[key] = str(e).splitlines()[0]
    games.sort(key=lambda g: (g.start_time, g.espn_event_id))
    return ScoreboardResult(games, errors)


def _roster_player(raw: Any, group: str) -> RosterPlayer:
    a = _AthleteRaw.model_validate(raw)
    name = a.fullName or a.displayName
    if not name:
        raise ValueError(f"athlete {a.id} has no name")
    status_type = (a.status.type if a.status else "").lower()
    unavailable = group in ("injuredReserveOrOut", "suspended") or status_type not in ("", "active")
    return RosterPlayer(a.id, name, a.position.abbreviation if a.position else "", group,
                        unavailable)


def parse_roster(payload: Any) -> list[RosterPlayer]:
    """NFL, NHL and MLB group athletes by position; NBA returns a flat list (verified)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("athletes"), list):
        raise SchemaError("roster has no 'athletes' list")
    players: dict[str, RosterPlayer] = {}
    try:
        for entry in payload["athletes"]:
            if isinstance(entry, dict) and "items" in entry:
                group = str(entry.get("position", ""))
                for raw in entry["items"]:
                    p = _roster_player(raw, group)
                    players.setdefault(p.espn_athlete_id, p)
            else:
                p = _roster_player(entry, "")
                players.setdefault(p.espn_athlete_id, p)
    except (ValidationError, ValueError, TypeError) as e:
        raise SchemaError(f"roster athlete: {str(e).splitlines()[0]}") from e
    return sorted(players.values(), key=lambda p: (p.unavailable, p.name))


# --- Fetching (web app) ----------------------------------------------------------------------

_router = None
_router_lock = threading.Lock()


def default_router():
    """The web app's router: the same failover and failure rules as the worker's, with
    in-memory breakers of its own (section 8.3)."""
    global _router
    from parlaytracker.ingest.router import Breakers, EspnRouter  # router imports this module
    with _router_lock:
        if _router is None:
            _router = EspnRouter(Breakers(engine=None))
        return _router


def reset_default_router() -> None:
    global _router
    with _router_lock:
        _router = None


def _as_fetch_error(path: str, e: Exception) -> FetchError:
    from parlaytracker.ingest.router import AllProvidersFailed
    kind = e.kind if isinstance(e, AllProvidersFailed) and e.kind else FailureKind.TRANSIENT
    return FetchError(path, kind, str(e))


def fetch_scoreboard(sport: Sport, day: date, max_wait: float = 0.0) -> ScoreboardResult:
    from parlaytracker.ingest.router import AllProvidersFailed
    try:
        return default_router().scoreboard(sport, day, max_wait).value
    except AllProvidersFailed as e:
        raise _as_fetch_error(f"{SPORT_PATHS[sport]}/scoreboard", e) from e


def fetch_roster(sport: Sport, team_id: str, max_wait: float = 0.0) -> list[RosterPlayer]:
    from parlaytracker.ingest.router import AllProvidersFailed
    try:
        return default_router().roster(sport, team_id, max_wait).value
    except AllProvidersFailed as e:
        raise _as_fetch_error(f"{SPORT_PATHS[sport]}/teams/{team_id}/roster", e) from e


# --- Box scores (summary and cdn) ------------------------------------------------------------

# (box-score group names, column keys summed) per player market. Keys, never positions or
# labels (section 6.1). NHL has no points column: goals + assists. NBA's group has no name.
_NO_NAME = ""
STAT_COLUMNS: dict[Sport, dict[MarketType, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    Sport.NFL: {
        MarketType.PLAYER_RECEPTIONS: (("receiving",), ("receptions",)),
        MarketType.PLAYER_RECEIVING_YARDS: (("receiving",), ("receivingYards",)),
        MarketType.PLAYER_RUSHING_YARDS: (("rushing",), ("rushingYards",)),
        MarketType.PLAYER_PASSING_YARDS: (("passing",), ("passingYards",)),
    },
    Sport.NBA: {MarketType.PLAYER_POINTS: ((_NO_NAME,), ("points",))},
    Sport.NHL: {MarketType.PLAYER_POINTS: (("forwards", "defenses"), ("goals", "assists"))},
    Sport.MLB: {},
}


class _BoxTeamIdRaw(BaseModel):
    id: str


class _BoxAthleteIdRaw(BaseModel):
    id: str


class _BoxCompetitorRaw(BaseModel):
    homeAway: Literal["home", "away"]
    score: str | None = None
    id: str | None = None
    team: _BoxTeamIdRaw | None = None


class _BoxCompetitionRaw(BaseModel):
    competitors: list[_BoxCompetitorRaw]
    status: _StatusRaw


class _BoxHeaderRaw(BaseModel):
    id: str
    competitions: list[_BoxCompetitionRaw]


class _BoxAthleteRaw(BaseModel):
    athlete: _BoxAthleteIdRaw
    stats: list[str] = []
    didNotPlay: bool = False


class _BoxGroupRaw(BaseModel):
    name: str | None = None
    keys: list[str] = []
    athletes: list[_BoxAthleteRaw] = []


class _BoxTeamPlayersRaw(BaseModel):
    statistics: list[_BoxGroupRaw] = []


class _BoxRaw(BaseModel):
    players: list[_BoxTeamPlayersRaw] = []


class _BoxDocRaw(BaseModel):
    header: _BoxHeaderRaw
    boxscore: _BoxRaw


def unwrap_cdn(payload: Any) -> Any:
    """cdn.espn.com wraps the summary document in `gamepackageJSON` (verified, section 6.1)."""
    if isinstance(payload, dict) and "gamepackageJSON" in payload:
        return payload["gamepackageJSON"]
    return payload


def _stat_value(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise ValueError(f"not a number: {text!r}") from e
    if not value.is_finite():
        raise ValueError(f"not a number: {text!r}")
    return value


def _team_id(c: _BoxCompetitorRaw) -> str:
    team_id = c.team.id if c.team else c.id
    if team_id is None:
        raise ValueError("competitor has no team id")
    return team_id


def parse_box_score(sport: Sport, payload: Any) -> BoxScore:
    """Parse a summary or cdn document. Raises SchemaError if it isn't one we understand."""
    try:
        doc = _BoxDocRaw.model_validate(unwrap_cdn(payload))
        header = doc.header
        if len(header.competitions) != 1:
            raise ValueError(f"expected 1 competition, got {len(header.competitions)}")
        comp = header.competitions[0]
        sides = {c.homeAway: c for c in comp.competitors}
        if set(sides) != {"home", "away"}:
            raise ValueError("expected one home and one away competitor")
        state = comp.status.type.state
        stats: dict[MarketType, dict[str, Decimal]] = {m: {} for m in STAT_COLUMNS[sport]}
        did_not_play: set[str] = set()
        for team in doc.boxscore.players:
            for group in team.statistics:
                for market, (groups, columns) in STAT_COLUMNS[sport].items():
                    if (group.name or _NO_NAME) not in groups:
                        continue
                    idx = [group.keys.index(c) for c in columns]  # ValueError if a key is gone
                    for a in group.athletes:
                        if a.didNotPlay:
                            did_not_play.add(a.athlete.id)
                        elif a.stats:
                            if len(a.stats) != len(group.keys):
                                raise ValueError(f"athlete {a.athlete.id} has {len(a.stats)} "
                                                 f"stats for {len(group.keys)} keys")
                            stats[market][a.athlete.id] = sum(
                                (_stat_value(a.stats[i]) for i in idx), Decimal(0))
        return BoxScore(
            espn_event_id=header.id,
            status=map_status(comp.status.type.name, state, comp.status.type.completed),
            home_espn_team_id=_team_id(sides["home"]), away_espn_team_id=_team_id(sides["away"]),
            home_score=_score(sides["home"].score, state),
            away_score=_score(sides["away"].score, state),
            stats=stats, did_not_play=frozenset(did_not_play),
        )
    except (ValidationError, ValueError, TypeError) as e:
        raise SchemaError(f"box score: {str(e).splitlines()[0]}") from e

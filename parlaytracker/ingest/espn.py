"""ESPN client and parsers (SPEC.md section 6.1).

Nothing outside this module touches ESPN's raw JSON: parsers validate the parts they use
with Pydantic and return typed dataclasses. A malformed event is reported for that event
only; a malformed document raises SchemaError.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from parlaytracker.core.models import DataSource, EventStatus, FailureKind, Sport
from parlaytracker.ingest.http import FetchError, RateLimiter, get_json

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
BASE_PATH = "/apis/site/v2/sports"
SPORT_PATHS = {
    Sport.NFL: "football/nfl",
    Sport.NBA: "basketball/nba",
    Sport.MLB: "baseball/mlb",
    Sport.NHL: "hockey/nhl",
}
# Tried in order (section 6.1). cdn.espn.com serves a different document shape and is added
# with the summary parser in Phase 4.
HOSTS: list[tuple[DataSource, str]] = [
    (DataSource.ESPN_WEB, "https://site.web.api.espn.com"),
    (DataSource.ESPN_SITE, "https://site.api.espn.com"),
]

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


# --- Fetching ------------------------------------------------------------------------------


def fetch_json(path: str, params: dict[str, str] | None = None,
               max_wait: float = 0.0) -> tuple[DataSource, Any]:
    """GET `path` from the first ESPN host that answers (section 6.1). Returns the provider.

    Every host failing raises the last FetchError. The Phase 4 router adds circuit breakers.
    """
    last: FetchError | None = None
    for provider, host in HOSTS:
        try:
            return provider, get_json(f"{host}{BASE_PATH}/{path}", params, LIMITER, max_wait)
        except FetchError as e:
            log.warning("ESPN %s failed: %s", provider, e)
            last = e
    assert last is not None
    raise last


def fetch_scoreboard(sport: Sport, day: date, max_wait: float = 0.0) -> ScoreboardResult:
    _, payload = fetch_json(f"{SPORT_PATHS[sport]}/scoreboard",
                            {"dates": day.strftime("%Y%m%d")}, max_wait)
    try:
        return parse_scoreboard(sport, payload)
    except SchemaError as e:
        raise FetchError(f"{SPORT_PATHS[sport]}/scoreboard", FailureKind.SCHEMA, str(e)) from e


def fetch_roster(sport: Sport, team_id: str, max_wait: float = 0.0) -> list[RosterPlayer]:
    path = f"{SPORT_PATHS[sport]}/teams/{team_id}/roster"
    _, payload = fetch_json(path, None, max_wait)
    try:
        return parse_roster(payload)
    except SchemaError as e:
        raise FetchError(path, FailureKind.SCHEMA, str(e)) from e

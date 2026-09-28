"""The Odds API client and parsers: closing lines only (SPEC.md sections 6.2 and 8.2).

Every call goes through the shared HTTP layer and the `odds_api` circuit breaker. The API key
is a query parameter, so nothing here may log a URL with its parameters.
"""
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

from parlaytracker.core.models import FailureKind, Sport
from parlaytracker.ingest.http import FetchError, RateLimited, RateLimiter, get_json_response
from parlaytracker.ingest.resolve import SPORT_KEYS
from parlaytracker.ingest.router import Breakers

log = logging.getLogger("parlaytracker.odds_api")

BASE_URL = "https://api.the-odds-api.com/v4"
SOURCE = "odds_api"


class ApiEvent(BaseModel):
    id: str
    commence_time: datetime
    home_team: str
    away_team: str


class ApiOutcome(BaseModel):
    name: str  # "Over" / "Under", or a team name for spreads
    description: str | None = None  # the player, or the team for team totals
    price: int  # American, because we ask for oddsFormat=american
    point: float | None = None


class ApiMarket(BaseModel):
    key: str
    outcomes: list[ApiOutcome]


class ApiBookmaker(BaseModel):
    key: str
    markets: list[ApiMarket] = []


class EventOdds(BaseModel):
    id: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: list[ApiBookmaker] = []


_EVENTS = TypeAdapter(list[ApiEvent])
_EVENT_ODDS = TypeAdapter(EventOdds)


class RequestRejected(Exception):
    """The API refused this request (for example an unknown market): not the provider's fault,
    so it doesn't count against the breaker."""


class OddsSource(Protocol):
    """What `capture_closing` needs; tests supply a fake."""

    quota_remaining: int | None

    def events(self, sport: Sport) -> list[ApiEvent]: ...

    def event_odds(self, sport: Sport, event_id: str, markets: Sequence[str]) -> EventOdds: ...


class OddsApiClient:
    def __init__(self, api_key: str, breakers: Breakers, limiter: RateLimiter | None = None):
        self._api_key = api_key
        self._breakers = breakers
        # The worker is the only caller; a generous per-minute limit, never tight enough to
        # delay a capture.
        self._limiter = limiter or RateLimiter(per_host_interval=1.0, per_minute=30)
        self.quota_remaining: int | None = breakers[SOURCE].quota_remaining
        self.last_cost: int | None = None
        self.calls = 0
        self._raw = ""  # the last body, saved if it turns out not to fit the model

    def events(self, sport: Sport) -> list[ApiEvent]:
        """Upcoming games for a sport. Costs 0 credits."""
        data = self._get(f"/sports/{SPORT_KEYS[sport]}/events", {})
        return self._parse(_EVENTS, data)

    def event_odds(self, sport: Sport, event_id: str, markets: Sequence[str]) -> EventOdds:
        """One game's odds for `markets`, US books only. Costs len(markets) credits."""
        params = {"regions": "us", "oddsFormat": "american", "markets": ",".join(markets)}
        data = self._get(f"/sports/{SPORT_KEYS[sport]}/events/{event_id}/odds", params)
        return self._parse(_EVENT_ODDS, data)

    def _get(self, path: str, params: dict[str, str]):
        self._breakers.allow(SOURCE)  # raises ProviderOpen
        url = BASE_URL + path
        self.calls += 1
        try:
            response = get_json_response(url, {**params, "apiKey": self._api_key}, self._limiter)
        except RateLimited:
            raise
        except FetchError as e:
            if e.status_code == 422:
                raise RequestRejected(f"HTTP 422 for {path}: {(e.body or '')[:200]}") from e
            self._breakers.failure(SOURCE, e.kind, e.detail, e.retry_after)
            raise
        self._read_quota(response.headers)
        self._raw = response.text
        return response.data

    def _read_quota(self, headers) -> None:
        for name, attr in (("x-requests-remaining", "quota_remaining"),
                           ("x-requests-last", "last_cost")):
            try:
                setattr(self, attr, int(float(headers[name])))
            except (KeyError, ValueError):
                pass
        self._breakers.success(SOURCE, self.quota_remaining)

    def _parse(self, adapter: TypeAdapter, data):
        try:
            return adapter.validate_python(data)
        except ValidationError as e:
            # A 200 that doesn't fit the model: retrying won't fix a format change.
            self._breakers.failure(SOURCE, FailureKind.SCHEMA, f"{e.error_count()} errors")
            raise FetchError(BASE_URL, FailureKind.SCHEMA, "response does not match the model",
                             body=self._raw[:1_000_000]) from e


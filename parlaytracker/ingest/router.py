"""Per-provider circuit breakers and the ESPN router (SPEC.md section 8.3).

`Breakers` keeps one breaker per provider and persists it to `source_health`. `EspnRouter`
sends each ESPN request to the first provider whose breaker isn't open, falls through to the
next one in the same run, classifies every failure, and saves raw samples.
"""
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from parlaytracker.core.models import DataSource, FailureKind, HealthState, SourceHealth, Sport
from parlaytracker.ingest import espn, guards
from parlaytracker.ingest.http import FetchError, JsonResponse, RateLimited, get_json_response

log = logging.getLogger("parlaytracker.breaker")

PROVIDERS = ("espn_web", "espn_site", "espn_cdn", "nflverse", "odds_api")

TRANSIENT_OPEN_AFTER = 3  # consecutive failures
TRANSIENT_BASE = timedelta(seconds=30)
TRANSIENT_MAX = timedelta(minutes=5)
BLOCKED_FOR = timedelta(minutes=10)
THROTTLED_DEFAULT = timedelta(minutes=5)
SCHEMA_FOR = timedelta(minutes=30)  # also `implausible`: retrying won't fix a format change
FROZEN_FOR = timedelta(minutes=5)


class ProviderOpen(Exception):
    """The provider's breaker is open: the request was not made."""

    def __init__(self, source: str, open_until: datetime | None):
        super().__init__(f"{source} is unavailable until {open_until:%H:%M:%S}"
                         if open_until else f"{source} is unavailable")
        self.source = source
        self.open_until = open_until


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class CircuitBreaker:
    """Closed, open or half-open; a breaker always half-opens again, so nothing is abandoned.

    Not thread-safe by itself: the registry's lock guards every call.
    """

    def __init__(self, source: str, clock: Callable[[], datetime] = _utcnow):
        self.source = source
        self._clock = clock
        self.open_until: datetime | None = None  # set while open or half-open
        self.failure_kind: FailureKind | None = None
        self.consecutive_failures = 0
        self.last_success_at: datetime | None = None
        self.last_failure_at: datetime | None = None
        self.last_error: str | None = None
        self.quota_remaining: int | None = None  # Odds API only
        self._open_count = 0  # consecutive transient openings, for the doubling
        self._requests: deque[datetime] = deque()
        self._errors: deque[datetime] = deque()

    # --- state ---------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.open_until is not None and self._clock() < self.open_until

    @property
    def is_half_open(self) -> bool:
        return self.open_until is not None and self._clock() >= self.open_until

    @property
    def state(self) -> HealthState:
        if self.open_until is not None:
            return HealthState.OPEN  # still open, or half-open and waiting for its trial
        return HealthState.DEGRADED if self.consecutive_failures else HealthState.OK

    def allow(self) -> bool:
        """May a request go out? Once `open_until` passes, the next request is the trial."""
        return not self.is_open

    # --- outcomes --------------------------------------------------------------------------

    def record_success(self) -> bool:
        """Close the breaker and reset its counters. True if the persisted state changed."""
        now = self._clock()
        changed = self.open_until is not None or self.consecutive_failures > 0
        self._requests.append(now)
        self.last_success_at = now
        self.open_until = None
        self.failure_kind = None
        self.consecutive_failures = 0
        self._open_count = 0
        return changed

    def record_failure(self, kind: FailureKind, detail: str,
                       retry_after: float | None = None) -> bool:
        """Count a failure and open the breaker if its kind calls for it. True if it changed."""
        now = self._clock()
        trial = self.is_half_open
        self._requests.append(now)
        self._errors.append(now)
        self.last_failure_at = now
        self.last_error = detail[:500]
        self.consecutive_failures += 1
        duration = self._open_duration(kind, retry_after, trial)
        if duration is None:
            return True  # degraded, not open
        self.open_until = now + duration
        self.failure_kind = kind
        log.warning("%s: breaker open for %ds (%s: %s)", self.source, duration.total_seconds(),
                    kind, detail)
        return True

    def _open_duration(self, kind: FailureKind, retry_after: float | None,
                       trial: bool) -> timedelta | None:
        match kind:
            case FailureKind.TRANSIENT:
                if not trial and self.consecutive_failures < TRANSIENT_OPEN_AFTER:
                    return None
                duration = min(TRANSIENT_BASE * 2 ** self._open_count, TRANSIENT_MAX)
                self._open_count += 1
                return duration
            case FailureKind.BLOCKED:
                return BLOCKED_FOR
            case FailureKind.THROTTLED:
                return timedelta(seconds=retry_after) if retry_after else THROTTLED_DEFAULT
            case FailureKind.FROZEN:
                return FROZEN_FOR
            case _:  # schema, implausible
                return SCHEMA_FOR

    # --- counters and persistence --------------------------------------------------------

    def hourly_counts(self) -> tuple[int, int]:
        cutoff = self._clock() - timedelta(hours=1)
        for window in (self._requests, self._errors):
            while window and window[0] < cutoff:
                window.popleft()
        return len(self._requests), len(self._errors)

    def restore(self, row: SourceHealth) -> None:
        """Pick up where the last process stopped, so a restart doesn't cut a block short."""
        self.quota_remaining = row.quota_remaining
        self.last_success_at = row.last_success_at
        self.last_failure_at = row.last_failure_at
        self.last_error = row.last_error
        self.consecutive_failures = row.consecutive_failures
        if row.state is HealthState.OPEN and row.open_until is not None:
            self.open_until = row.open_until
            self.failure_kind = row.failure_kind


class Breakers:
    """One breaker per provider, written to `source_health` on every transition."""

    def __init__(self, engine: Engine | None = None,
                 clock: Callable[[], datetime] = _utcnow):
        self._engine = engine
        self._lock = threading.Lock()
        self._breakers = {name: CircuitBreaker(name, clock) for name in PROVIDERS}

    def __getitem__(self, source: str) -> CircuitBreaker:
        return self._breakers[source]

    def load(self) -> None:
        """Restore each breaker from its `source_health` row (worker startup)."""
        if self._engine is None:
            return
        with Session(self._engine) as session:
            for name, breaker in self._breakers.items():
                row = session.get(SourceHealth, name)
                if row is not None:
                    breaker.restore(row)

    def allow(self, source: str) -> None:
        """Raise ProviderOpen if `source` must be skipped."""
        with self._lock:
            breaker = self._breakers[source]
            if not breaker.allow():
                raise ProviderOpen(source, breaker.open_until)

    def success(self, source: str, quota_remaining: int | None = None) -> None:
        with self._lock:
            breaker = self._breakers[source]
            if quota_remaining is not None:
                breaker.quota_remaining = quota_remaining
            changed = breaker.record_success()
        if changed:
            self.persist(source)

    def failure(self, source: str, kind: FailureKind, detail: str,
                retry_after: float | None = None) -> None:
        with self._lock:
            self._breakers[source].record_failure(kind, detail, retry_after)
        self.persist(source)

    def persist(self, *sources: str) -> None:
        """Write breaker state and the hourly counters; a failed write is logged, not raised."""
        if self._engine is None:
            return
        try:
            with self._lock:
                values = [self._row(self._breakers[s]) for s in (sources or PROVIDERS)]
            with self._engine.begin() as conn:
                for row in values:
                    stmt = insert(SourceHealth).values(**row)
                    update = {k: v for k, v in row.items() if k != "source"}
                    if row["quota_remaining"] is None:
                        del update["quota_remaining"]  # never wipe a known quota
                    conn.execute(stmt.on_conflict_do_update(
                        index_elements=[SourceHealth.source], set_=update))
        except Exception:
            log.exception("could not write source_health")

    @staticmethod
    def _row(b: CircuitBreaker) -> dict:
        requests, errors = b.hourly_counts()
        return {
            "source": b.source, "state": b.state, "failure_kind": b.failure_kind,
            "open_until": b.open_until, "last_success_at": b.last_success_at,
            "last_failure_at": b.last_failure_at,
            "consecutive_failures": b.consecutive_failures, "requests_last_hour": requests,
            "errors_last_hour": errors, "last_error": b.last_error,
            "quota_remaining": b.quota_remaining,
        }


# --- The ESPN router -------------------------------------------------------------------------

MAX_SAMPLE_BYTES = 1_000_000


@dataclass(frozen=True)
class SampleRecord:
    """A raw response worth keeping: a parse failure, or a recording of a watched event."""
    source: str
    reason: str  # "failure" or "recording"
    url: str
    status_code: int | None
    espn_event_id: str | None
    error: str | None
    body: str


def other_score_provider(current: DataSource) -> DataSource:
    """The provider a frozen-feed probe asks: the other scoreboard host (section 8.3)."""
    return DataSource.ESPN_SITE if current is DataSource.ESPN_WEB else DataSource.ESPN_WEB


class AllProvidersFailed(Exception):
    """Every ESPN provider was skipped or failed. `errors` says why, per provider."""

    def __init__(self, errors: dict[str, str], kind: FailureKind | None = None):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()) or "no provider")
        self.errors = errors
        self.kind = kind  # the last failure's kind, if any request actually failed


@dataclass(frozen=True)
class Routed[T]:
    provider: DataSource
    value: T


# Which providers can answer which request, in order (section 6.1). The cdn host serves box
# scores only.
_SCORE_PROVIDERS = (DataSource.ESPN_WEB, DataSource.ESPN_SITE)
_BOX_PROVIDERS = (DataSource.ESPN_WEB, DataSource.ESPN_SITE, DataSource.ESPN_CDN)
_HOSTS = dict(espn.HOSTS)


class EspnRouter:
    """The worker builds one with the DB-backed `Breakers` and a sample sink; the web app has
    its own with in-memory breakers."""

    def __init__(self, breakers: Breakers, limiter=None,
                 sample_sink: Callable[[SampleRecord], None] | None = None,
                 record_event_ids: frozenset[str] | set[str] = frozenset()):
        self._breakers = breakers
        self._limiter = limiter or espn.LIMITER  # looked up now, so tests can swap it
        self._sink = sample_sink
        self._record = frozenset(record_event_ids)

    @property
    def breakers(self) -> Breakers:
        return self._breakers

    # --- public requests -------------------------------------------------------------------

    def scoreboard(self, sport: Sport, day: date, max_wait: float = 0.0, *,
                   only: DataSource | None = None) -> Routed[espn.ScoreboardResult]:
        """A day's scoreboard from the first working provider, or from `only` (the frozen-feed
        probe asks the other provider, section 8.3)."""
        path = f"{espn.SPORT_PATHS[sport]}/scoreboard"

        def check(result: espn.ScoreboardResult) -> None:
            for g in result.games:
                guards.check_score(sport, g.home_score)
                guards.check_score(sport, g.away_score)

        params = {"dates": day.strftime("%Y%m%d")}
        return self._route(
            _SCORE_PROVIDERS, lambda p, w: self._get_site(p, path, params, w),
            lambda payload: espn.parse_scoreboard(sport, payload), check, max_wait,
            recording_id=None, watch=self._record, only=only)

    def box_score(self, sport: Sport, espn_event_id: str, max_wait: float = 0.0, *,
                  only: DataSource | None = None) -> Routed[espn.BoxScore]:
        """The box score from the first working provider, or from `only` (the canary)."""
        path = f"{espn.SPORT_PATHS[sport]}/summary"

        def fetch(provider: DataSource, wait: float) -> tuple[str, JsonResponse]:
            if provider is DataSource.ESPN_CDN:
                url = espn.CDN_URL.format(league=espn.CDN_LEAGUES[sport])
                params = {"xhr": "1", "gameId": espn_event_id}
                return url, get_json_response(url, params, self._limiter, wait)
            return self._get_site(provider, path, {"event": espn_event_id}, wait)

        def check(box: espn.BoxScore) -> None:
            guards.check_score(sport, box.home_score)
            guards.check_score(sport, box.away_score)
            guards.check_stats(box.stats)

        return self._route(_BOX_PROVIDERS, fetch,
                           lambda payload: espn.parse_box_score(sport, payload), check, max_wait,
                           recording_id=espn_event_id, only=only)

    def roster(self, sport: Sport, team_id: str, max_wait: float = 0.0
               ) -> Routed[list[espn.RosterPlayer]]:
        path = f"{espn.SPORT_PATHS[sport]}/teams/{team_id}/roster"
        return self._route(_SCORE_PROVIDERS, lambda p, w: self._get_site(p, path, None, w),
                           espn.parse_roster, lambda _: None, max_wait, recording_id=None)

    # --- internals -------------------------------------------------------------------------

    def _get_site(self, provider: DataSource, path: str, params: dict[str, str] | None,
                  wait: float) -> tuple[str, JsonResponse]:
        url = f"{_HOSTS[provider]}{espn.BASE_PATH}/{path}"
        return url, get_json_response(url, params, self._limiter, wait)

    def _route[T](self, providers: tuple[DataSource, ...],
                  fetch: Callable[[DataSource, float], tuple[str, JsonResponse]],
                  parse: Callable[[Any], T], check: Callable[[T], None], max_wait: float,
                  recording_id: str | None, watch: frozenset[str] = frozenset(),
                  only: DataSource | None = None) -> Routed[T]:
        errors: dict[str, str] = {}
        limited = False
        last_kind: FailureKind | None = None
        for provider in providers:
            if only is not None and provider is not only:
                continue
            source = provider.value
            try:
                self._breakers.allow(source)
            except ProviderOpen as e:
                errors[source] = str(e)
                continue
            url = ""
            try:
                url, response = fetch(provider, max_wait)
            except RateLimited as e:
                limited = True
                errors[source] = str(e)
                continue
            except FetchError as e:
                last_kind = e.kind
                self._breakers.failure(source, e.kind, e.detail, e.retry_after)
                errors[source] = str(e)
                continue
            try:
                value = parse(response.data)
                check(value)
            except (espn.SchemaError, guards.ImplausibleError) as e:
                kind = (FailureKind.SCHEMA if isinstance(e, espn.SchemaError)
                        else FailureKind.IMPLAUSIBLE)
                last_kind = kind
                self._breakers.failure(source, kind, str(e))
                self._save(source, "failure", url, response, None, str(e))
                errors[source] = f"{kind}: {e}"
                continue
            self._breakers.success(source)
            watched = recording_id if recording_id in self._record else next(
                (w for w in sorted(watch) if w in response.text), None)
            if watched is not None:  # tagged with the watched event, so a game can be exported
                self._save(source, "recording", url, response, watched, None)
            return Routed(provider, value)
        if limited and last_kind is None:
            raise RateLimited("espn", 0.0)  # skipped, not failed: the next run tries again
        raise AllProvidersFailed(errors, last_kind)

    def _save(self, source: str, reason: str, url: str, response: JsonResponse,
              event_id: str | None, error: str | None) -> None:
        if self._sink is None:
            return
        try:
            self._sink(SampleRecord(source, reason, url, None, event_id, error,
                                    response.text[:MAX_SAMPLE_BYTES]))
        except Exception:
            log.exception("could not save a raw sample")

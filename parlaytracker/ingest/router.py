"""Per-provider circuit breakers (SPEC.md section 8.3).

Phase 3 provides the breakers and their persistence to `source_health`. Phase 4 adds the ESPN
router on top: host failover, integrity guards and raw samples.
"""
import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from parlaytracker.core.models import FailureKind, HealthState, SourceHealth

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

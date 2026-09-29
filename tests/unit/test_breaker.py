"""Circuit breaker rules (SPEC.md section 8.3)."""
from datetime import UTC, datetime, timedelta

import pytest

from parlaytracker.core.models import FailureKind, HealthState
from parlaytracker.ingest.router import Breakers, CircuitBreaker, ProviderOpen


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def breaker(clock) -> CircuitBreaker:
    return CircuitBreaker("odds_api", clock)


def open_seconds(b: CircuitBreaker, clock: Clock) -> float:
    return (b.open_until - clock.now).total_seconds()


def test_transient_failures_open_only_after_three_in_a_row(breaker, clock):
    breaker.record_failure(FailureKind.TRANSIENT, "timeout")
    breaker.record_failure(FailureKind.TRANSIENT, "timeout")
    assert breaker.allow() and breaker.state is HealthState.DEGRADED
    breaker.record_failure(FailureKind.TRANSIENT, "timeout")
    assert not breaker.allow() and breaker.state is HealthState.OPEN
    assert open_seconds(breaker, clock) == 30


def test_a_success_resets_the_transient_count(breaker):
    breaker.record_failure(FailureKind.TRANSIENT, "x")
    breaker.record_failure(FailureKind.TRANSIENT, "x")
    breaker.record_success()
    breaker.record_failure(FailureKind.TRANSIENT, "x")
    assert breaker.allow() and breaker.consecutive_failures == 1


def test_transient_open_time_doubles_up_to_five_minutes(breaker, clock):
    for _ in range(3):
        breaker.record_failure(FailureKind.TRANSIENT, "x")
    seen = [open_seconds(breaker, clock)]
    for _ in range(6):
        clock.advance(seen[-1])  # half-open: the next request is the trial, and it fails
        assert breaker.allow()
        breaker.record_failure(FailureKind.TRANSIENT, "x")
        seen.append(open_seconds(breaker, clock))
    assert seen == [30, 60, 120, 240, 300, 300, 300]


def test_half_open_trial_success_closes_and_resets(breaker, clock):
    for _ in range(3):
        breaker.record_failure(FailureKind.TRANSIENT, "x")
    assert not breaker.allow()
    clock.advance(31)
    assert breaker.allow()  # the trial
    breaker.record_success()
    assert breaker.state is HealthState.OK and breaker.open_until is None
    for _ in range(3):
        breaker.record_failure(FailureKind.TRANSIENT, "x")
    assert open_seconds(breaker, clock) == 30  # back to the first duration


@pytest.mark.parametrize(("kind", "retry_after", "seconds"), [
    (FailureKind.BLOCKED, None, 600),
    (FailureKind.BLOCKED, 5, 600),
    (FailureKind.THROTTLED, None, 300),
    (FailureKind.THROTTLED, 42, 42),
    (FailureKind.SCHEMA, None, 1800),
    (FailureKind.IMPLAUSIBLE, None, 1800),
    (FailureKind.FROZEN, None, 300),
])
def test_other_failures_open_at_once_for_their_own_time(breaker, clock, kind, retry_after,
                                                        seconds):
    breaker.record_failure(kind, "x", retry_after)
    assert not breaker.allow()
    assert breaker.failure_kind is kind
    assert open_seconds(breaker, clock) == seconds


def test_a_breaker_always_half_opens_again(breaker, clock):
    breaker.record_failure(FailureKind.SCHEMA, "x")
    clock.advance(1799)
    assert not breaker.allow()
    clock.advance(2)
    assert breaker.allow()


def test_hourly_counters_forget_old_requests(breaker, clock):
    breaker.record_success()
    breaker.record_failure(FailureKind.TRANSIENT, "x")
    assert breaker.hourly_counts() == (2, 1)
    clock.advance(3601)
    assert breaker.hourly_counts() == (0, 0)


def test_breakers_raise_provider_open_without_a_database(clock):
    breakers = Breakers(engine=None, clock=clock)
    breakers.allow("odds_api")
    breakers.failure("odds_api", FailureKind.BLOCKED, "403")
    with pytest.raises(ProviderOpen) as e:
        breakers.allow("odds_api")
    assert e.value.source == "odds_api"
    breakers.allow("espn_web")  # one breaker per provider

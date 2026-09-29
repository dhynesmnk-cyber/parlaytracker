"""The worker: single-instance lock, heartbeat, scheduler and breaker persistence (SPEC.md 8)."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from parlaytracker.core.models import FailureKind, HealthState, SourceHealth
from parlaytracker.ingest.odds_api import OddsApiClient
from parlaytracker.ingest.router import Breakers
from parlaytracker.worker.__main__ import build_scheduler, try_lock
from parlaytracker.worker.jobs import guarded, heartbeat

pytestmark = pytest.mark.db


@pytest.fixture
def clean_health(engine: Engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM source_health"))


def worker_row(engine: Engine) -> SourceHealth:
    with Session(engine) as s:
        return s.scalars(select(SourceHealth).where(SourceHealth.source == "worker")).one()


def test_heartbeat_creates_then_updates_the_row(engine, clean_health):
    first = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    heartbeat(engine, first)
    assert worker_row(engine).last_success_at == first
    later = datetime(2026, 10, 4, 12, 1, tzinfo=UTC)
    heartbeat(engine, later)
    row = worker_row(engine)
    assert (row.last_success_at, row.state) == (later, HealthState.OK)


def health(engine: Engine, source: str) -> SourceHealth:
    with Session(engine) as s:
        return s.get(SourceHealth, source)


def test_heartbeat_writes_every_providers_breaker_state(engine, clean_health):
    breakers = Breakers(engine)
    breakers.failure("odds_api", FailureKind.BLOCKED, "403")
    heartbeat(engine, breakers=breakers)
    with Session(engine) as s:
        rows = {r.source: r for r in s.scalars(select(SourceHealth))}
    assert set(rows) == {"worker", "espn_web", "espn_site", "espn_cdn", "nflverse", "odds_api"}
    assert (rows["odds_api"].state, rows["odds_api"].failure_kind) == (
        HealthState.OPEN, FailureKind.BLOCKED)
    assert rows["odds_api"].open_until > datetime.now(tz=UTC) + timedelta(minutes=9)
    assert (rows["odds_api"].requests_last_hour, rows["odds_api"].errors_last_hour) == (1, 1)
    assert rows["espn_web"].state is HealthState.OK


def test_a_breaker_transition_is_written_at_once_and_its_quota_survives(engine, clean_health):
    breakers = Breakers(engine)
    breakers.success("odds_api", quota_remaining=412)
    breakers.persist("odds_api")
    assert health(engine, "odds_api").quota_remaining == 412
    breakers.failure("odds_api", FailureKind.SCHEMA, "format changed")  # no heartbeat needed
    row = health(engine, "odds_api")
    assert (row.state, row.failure_kind) == (HealthState.OPEN, FailureKind.SCHEMA)
    assert row.quota_remaining == 412  # a breaker that never saw a quota can't wipe it


def test_a_restart_keeps_an_open_breaker_and_the_quota(engine, clean_health):
    first = Breakers(engine)
    first.success("odds_api", quota_remaining=300)
    first.failure("odds_api", FailureKind.THROTTLED, "429", retry_after=600)
    second = Breakers(engine)
    second.load()
    assert not second["odds_api"].allow()
    assert second["odds_api"].failure_kind is FailureKind.THROTTLED
    assert second["odds_api"].quota_remaining == 300
    assert OddsApiClient("k", second).quota_remaining == 300
    assert second["espn_web"].allow()


def test_recovery_is_written_when_the_trial_succeeds(engine, clean_health):
    breakers = Breakers(engine)
    breakers.failure("odds_api", FailureKind.SCHEMA, "x")
    breakers["odds_api"].open_until = datetime.now(tz=UTC) - timedelta(seconds=1)  # half-open
    breakers.allow("odds_api")
    breakers.success("odds_api")
    row = health(engine, "odds_api")
    assert (row.state, row.open_until, row.consecutive_failures) == (HealthState.OK, None, 0)


def test_a_failing_job_is_recorded_and_never_raises(engine, clean_health):
    def boom():
        raise RuntimeError("kaput")

    guarded(engine, "capture_closing", boom)()  # must not raise
    assert "capture_closing: kaput" in health(engine, "worker").last_error


def test_the_scheduler_registers_the_jobs_with_the_spec_defaults(engine):
    breakers = Breakers(engine)
    scheduler = build_scheduler(engine, breakers, OddsApiClient("k", breakers), reserve=50)
    assert {j.id for j in scheduler.get_jobs()} == {
        "heartbeat", "capture_closing", "poll_nfl_live", "check_finals", "settle",
        "recheck_settled",
        "verify_nfl", "canary", "canary_at_startup", "prune_samples"}
    # Jobs added before start() are pending; the defaults apply to each when it is scheduled.
    assert scheduler._job_defaults == {"coalesce": True, "max_instances": 1,
                                       "misfire_grace_time": 30}
    every = {j.id: j.trigger.interval for j in scheduler.get_jobs()
             if hasattr(j.trigger, "interval")}
    assert every == {
        "heartbeat": timedelta(seconds=60), "capture_closing": timedelta(seconds=60),
        "poll_nfl_live": timedelta(seconds=30), "check_finals": timedelta(minutes=15),
        "settle": timedelta(minutes=5),
        "recheck_settled": timedelta(hours=1)}
    daily = {j.id: str(j.trigger) for j in scheduler.get_jobs() if j.id not in every
             and j.id != "canary_at_startup"}
    assert daily == {
        "verify_nfl": "cron[hour='10', minute='0']",
        "canary": "cron[hour='9', minute='0']",
        "prune_samples": "cron[hour='4', minute='0']"}
    zones = {j.id: str(j.trigger.timezone) for j in scheduler.get_jobs() if j.id in daily}
    assert zones == {"verify_nfl": "America/New_York", "canary": "America/New_York",
                     "prune_samples": "UTC"}


def test_without_an_odds_api_key_closing_lines_are_off_but_settlement_runs(engine):
    scheduler = build_scheduler(engine, Breakers(engine), None, reserve=50)
    ids = {j.id for j in scheduler.get_jobs()}
    assert "capture_closing" not in ids and {"heartbeat", "settle", "verify_nfl"} <= ids


def test_only_one_worker_can_hold_the_lock(engine):
    with engine.connect() as first, engine.connect() as second:
        assert try_lock(first) is True
        assert try_lock(second) is False
        first.execute(text("SELECT pg_advisory_unlock_all()"))
        first.commit()
        assert try_lock(second) is True
        second.execute(text("SELECT pg_advisory_unlock_all()"))
        second.commit()

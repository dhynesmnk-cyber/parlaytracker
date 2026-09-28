"""The Phase 2 worker: single-instance lock and heartbeat (SPEC.md section 8.1)."""
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from parlaytracker.core.models import HealthState, SourceHealth
from parlaytracker.worker.__main__ import heartbeat, run, try_lock

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


def test_run_beats_at_least_once_and_stops(engine, clean_health):
    stop = threading.Event()
    stop.set()
    run(engine, stop, interval=0.01)
    assert worker_row(engine).last_success_at is not None


def test_only_one_worker_can_hold_the_lock(engine):
    with engine.connect() as first, engine.connect() as second:
        assert try_lock(first) is True
        assert try_lock(second) is False
        first.execute(text("SELECT pg_advisory_unlock_all()"))
        first.commit()
        assert try_lock(second) is True
        second.execute(text("SELECT pg_advisory_unlock_all()"))
        second.commit()

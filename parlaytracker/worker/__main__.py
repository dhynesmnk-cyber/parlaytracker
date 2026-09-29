"""Background worker: `python -m parlaytracker.worker` (SPEC.md section 8).

Phases 3, 4 and 7 register `heartbeat`, `capture_closing`, `poll_nfl_live`, `check_finals`,
`settle`, `recheck_settled`, `verify_nfl`, `canary` and `prune_samples` (section 8.1).
"""
import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Connection, Engine, text

from parlaytracker.core.config import get_settings
from parlaytracker.core.db import make_engine
from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.odds_api import OddsApiClient
from parlaytracker.ingest.router import Breakers, EspnRouter
from parlaytracker.worker.jobs import ClosingCapture, guarded, heartbeat
from parlaytracker.worker.live import PollNflLive
from parlaytracker.worker.settle import (
    Canary,
    CheckFinals,
    RecheckSettled,
    Settle,
    VerifyNfl,
    prune_samples,
    sample_sink,
)

log = logging.getLogger("parlaytracker.worker")

# pg_try_advisory_lock key; any fixed number unique to this app. Two workers would poll twice
# and spend Odds API credits twice (section 8.1).
LOCK_KEY = 7_406_110_417
HEARTBEAT_SECONDS = 60
CAPTURE_SECONDS = 60
LIVE_SECONDS = 30
CHECK_FINALS_MINUTES = 15
SETTLE_MINUTES = 5
RECHECK_MINUTES = 60
ET = "America/New_York"


def try_lock(conn: Connection) -> bool:
    """Take the session-level advisory lock on `conn`; it lasts as long as the connection."""
    locked = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    conn.commit()
    return bool(locked)


def build_scheduler(engine: Engine, breakers: Breakers, odds: OddsApiClient | None,
                    reserve: int, record_event_ids: frozenset[str] = frozenset()
                    ) -> BlockingScheduler:
    """Every job runs with coalesce=True, max_instances=1, misfire_grace_time=30."""
    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30})
    router = EspnRouter(breakers, sample_sink=sample_sink(engine),
                        record_event_ids=record_event_ids)

    def nflverse() -> NflverseData:
        return NflverseData(breakers)  # a fresh one per run: each dataset loads once per run

    def add(name: str, job, trigger: str, **when) -> None:
        scheduler.add_job(guarded(engine, name, job), trigger, id=name, name=name, **when)

    add("heartbeat", lambda: heartbeat(engine, breakers=breakers), "interval",
        seconds=HEARTBEAT_SECONDS)
    if odds is not None:
        add("capture_closing", ClosingCapture(engine, odds, breakers, reserve), "interval",
            seconds=CAPTURE_SECONDS)
    add("poll_nfl_live", PollNflLive(engine, router), "interval", seconds=LIVE_SECONDS)
    add("check_finals", CheckFinals(engine, router), "interval", minutes=CHECK_FINALS_MINUTES)
    add("settle", Settle(engine, router), "interval", minutes=SETTLE_MINUTES)
    add("recheck_settled", RecheckSettled(engine, router), "interval", minutes=RECHECK_MINUTES)
    add("verify_nfl", VerifyNfl(engine, nflverse), "cron", hour=10, minute=0, timezone=ET)
    canary = Canary(router, nflverse)
    add("canary", canary, "cron", hour=9, minute=0, timezone=ET)
    add("canary_at_startup", canary, "date")  # run_date omitted: as soon as the scheduler starts
    add("prune_samples", lambda: prune_samples(engine), "cron", hour=4, minute=0, timezone="UTC")
    return scheduler


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine()
    lock_conn = engine.connect()  # held open for the life of the process
    if not try_lock(lock_conn):
        log.error("another worker already holds the lock; exiting")
        return 1

    settings = get_settings()
    breakers = Breakers(engine)
    breakers.load()
    odds = None
    if settings.odds_api_key is not None:
        odds = OddsApiClient(settings.odds_api_key.get_secret_value(), breakers)
    else:
        log.warning("ODDS_API_KEY is not set: closing lines will not be captured")

    scheduler = build_scheduler(engine, breakers, odds, settings.odds_api_reserve,
                                frozenset(settings.record_event_ids))
    signal.signal(signal.SIGTERM, lambda *_: scheduler.shutdown(wait=False))
    signal.signal(signal.SIGINT, lambda *_: scheduler.shutdown(wait=False))
    guarded(engine, "heartbeat", lambda: heartbeat(engine, breakers=breakers))()  # at once
    log.info("worker started (closing lines %s)", "on" if odds else "off: no ODDS_API_KEY")
    scheduler.start()  # blocks until shutdown
    log.info("worker stopped")
    lock_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

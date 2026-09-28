"""Background worker: `python -m parlaytracker.worker` (SPEC.md section 8).

Phase 3 runs two jobs: `heartbeat` and `capture_closing`. Later phases add the rest of the
table in section 8.1.
"""
import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Connection, Engine, text

from parlaytracker.core.config import get_settings
from parlaytracker.core.db import make_engine
from parlaytracker.ingest.odds_api import OddsApiClient
from parlaytracker.ingest.router import Breakers
from parlaytracker.worker.jobs import ClosingCapture, guarded, heartbeat

log = logging.getLogger("parlaytracker.worker")

# pg_try_advisory_lock key; any fixed number unique to this app. Two workers would poll twice
# and spend Odds API credits twice (section 8.1).
LOCK_KEY = 7_406_110_417
HEARTBEAT_SECONDS = 60
CAPTURE_SECONDS = 60


def try_lock(conn: Connection) -> bool:
    """Take the session-level advisory lock on `conn`; it lasts as long as the connection."""
    locked = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    conn.commit()
    return bool(locked)


def build_scheduler(engine: Engine, breakers: Breakers, odds: OddsApiClient | None,
                    reserve: int) -> BlockingScheduler:
    """Every job runs with coalesce=True, max_instances=1, misfire_grace_time=30."""
    scheduler = BlockingScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30})
    scheduler.add_job(guarded(engine, "heartbeat", lambda: heartbeat(engine, breakers=breakers)),
                      "interval", seconds=HEARTBEAT_SECONDS, id="heartbeat", name="heartbeat")
    if odds is not None:
        capture = ClosingCapture(engine, odds, breakers, reserve)
        scheduler.add_job(guarded(engine, "capture_closing", capture), "interval",
                          seconds=CAPTURE_SECONDS, id="capture_closing", name="capture_closing")
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

    scheduler = build_scheduler(engine, breakers, odds, settings.odds_api_reserve)
    signal.signal(signal.SIGTERM, lambda *_: scheduler.shutdown(wait=False))
    signal.signal(signal.SIGINT, lambda *_: scheduler.shutdown(wait=False))
    guarded(engine, "heartbeat", lambda: heartbeat(engine, breakers=breakers))()  # at once
    log.info("worker started (Phase 3: heartbeat%s)", ", closing lines" if odds else "")
    scheduler.start()  # blocks until shutdown
    log.info("worker stopped")
    lock_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

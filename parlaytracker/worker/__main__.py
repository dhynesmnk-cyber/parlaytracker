"""Background worker: `python -m parlaytracker.worker` (SPEC.md section 8).

Phase 2 only holds the single-instance lock and writes a heartbeat, so the app can show when
the worker is down. Phase 3 adds the scheduler and its jobs.
"""
import logging
import signal
import sys
import threading
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text
from sqlalchemy.dialects.postgresql import insert

from parlaytracker.core.db import make_engine
from parlaytracker.core.models import HealthState, SourceHealth

log = logging.getLogger("parlaytracker.worker")

# pg_try_advisory_lock key; any fixed number unique to this app. Two workers would poll twice
# and spend Odds API credits twice (section 8.1).
LOCK_KEY = 7_406_110_417
HEARTBEAT_SECONDS = 60


def try_lock(conn: Connection) -> bool:
    """Take the session-level advisory lock on `conn`; it lasts as long as the connection."""
    locked = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar()
    conn.commit()
    return bool(locked)


def heartbeat(engine: Engine, now: datetime | None = None) -> None:
    now = now or datetime.now(tz=UTC)
    stmt = insert(SourceHealth).values(source="worker", state=HealthState.OK, last_success_at=now,
                                       consecutive_failures=0, requests_last_hour=0,
                                       errors_last_hour=0)
    stmt = stmt.on_conflict_do_update(index_elements=[SourceHealth.source],
                                      set_={"last_success_at": now, "state": HealthState.OK})
    with engine.begin() as conn:
        conn.execute(stmt)


def run(engine: Engine, stop: threading.Event, interval: float = HEARTBEAT_SECONDS) -> None:
    """Beat until `stop` is set. A failed beat is logged, never fatal."""
    while True:
        try:
            heartbeat(engine)
        except Exception:
            log.exception("heartbeat failed")
        if stop.wait(interval):
            return


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine()
    lock_conn = engine.connect()  # held open for the life of the process
    if not try_lock(lock_conn):
        log.error("another worker already holds the lock; exiting")
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker started (Phase 2: heartbeat only)")
    run(engine, stop)
    log.info("worker stopped")
    lock_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

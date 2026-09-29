"""Maintenance commands: `python -m parlaytracker.cli --help` (SPEC.md sections 8.3 and 13).

    export-sample <id> <path>   write a saved raw response out as a test fixture
    backfill                    settle everything logged before the worker could (Phase 4)
"""
import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from parlaytracker.core.db import make_engine
from parlaytracker.core.models import RawSample
from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.router import Breakers, EspnRouter
from parlaytracker.worker.__main__ import try_lock
from parlaytracker.worker.settle import CheckFinals, Settle, VerifyNfl, sample_sink


def export_sample(engine: Engine, sample_id: int, path: Path) -> int:
    """Write `raw_samples.body` to `path`, ready to drop into tests/fixtures/."""
    with Session(engine) as session:
        sample = session.get(RawSample, sample_id)
        if sample is None:
            print(f"no raw sample with id {sample_id}", file=sys.stderr)
            return 1
        path.write_text(sample.body)
        print(f"wrote {len(sample.body)} bytes ({sample.source}, {sample.reason}, "
              f"{sample.fetched_at:%Y-%m-%d %H:%M}) to {path}")
    return 0


def backfill(engine: Engine, router: EspnRouter, nflverse) -> int:
    """Find out which old games finished, settle their legs, then check NFL against nflverse.

    A game found final long after the fact is dated to its expected end, so the ten-minute
    gate (section 7.1) holds honestly. Safe to run more than once: settled legs are never
    touched.
    """
    finals = CheckFinals(engine, router)(force=True, backfill=True)
    settled = Settle(engine, router)()
    verified = VerifyNfl(engine, nflverse)()
    print(f"games not yet reachable: {finals.skipped}")
    print(f"settled from ESPN: {settled.settled}, flagged for Review: {settled.flagged}")
    print(f"NFL: settled from nflverse {verified.settled}, verified {verified.verified}, "
          f"flagged {verified.flagged}, left unverified {verified.skipped}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parlaytracker.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-sample", help="write a raw sample to a file")
    export.add_argument("id", type=int, help="raw_samples.id")
    export.add_argument("path", type=Path)
    commands.add_parser("backfill", help="settle everything logged since Phase 2")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine()
    if args.command == "export-sample":
        return export_sample(engine, args.id, args.path)

    lock_conn = engine.connect()  # the worker's lock: two writers would poll ESPN twice
    if not try_lock(lock_conn):
        print("the worker is running: stop it first (`deploy/compose.sh stop worker`), run the "
              "backfill, then start it again", file=sys.stderr)
        return 1
    breakers = Breakers(engine)
    breakers.load()
    router = EspnRouter(breakers, sample_sink=sample_sink(engine))
    try:
        return backfill(engine, router, lambda: NflverseData(breakers))
    finally:
        breakers.persist()
        lock_conn.close()


if __name__ == "__main__":
    sys.exit(main())

"""Maintenance commands: `python -m parlaytracker.cli --help` (SPEC.md sections 8.3 and 13).

    export-sample <id> <path>   write a saved raw response out as a test fixture
    backfill                    settle everything logged before the worker could (Phase 4)
    read-slip <image> [--save]  read a slip screenshot with Qwen; --save keeps the raw reply
"""
import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from parlaytracker.core.db import make_engine
from parlaytracker.core.models import RawSample
from parlaytracker.ingest.extraction import Extractor
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


def read_slip(extractor, image_path: Path, save: Path | None) -> int:
    """Read one screenshot exactly as the Screenshot page does and show what came back.

    With `save`, the model's raw reply is written there: real replies are what Phase 6's exit
    wants as fixtures in tests/fixtures/qwen/. A slip can show a stake or account details, so
    read the file before committing it.
    """
    from parlaytracker.ingest.extraction import ExtractionError, prepare_image

    try:
        image = prepare_image(image_path.read_bytes())
        slip = extractor.extract(image)
    except (OSError, ExtractionError) as e:
        print(f"couldn't read {image_path}: {e}", file=sys.stderr)
        return 1
    print(slip.model_dump_json(indent=2))
    if save is not None:
        save.write_text(extractor.last_reply or "")
        print(f"saved the raw reply to {save}: check it for anything private before committing")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parlaytracker.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-sample", help="write a raw sample to a file")
    export.add_argument("id", type=int, help="raw_samples.id")
    export.add_argument("path", type=Path)
    commands.add_parser("backfill", help="settle everything logged since Phase 2")
    read = commands.add_parser("read-slip", help="read a slip screenshot with Qwen")
    read.add_argument("image", type=Path)
    read.add_argument("--save", type=Path, help="write the raw model reply to this file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "read-slip":
        from parlaytracker.core.config import get_settings
        settings = get_settings()
        extractor = Extractor.from_settings(settings.qwen_api_key, settings.qwen_base_url,
                                            settings.qwen_vision_model)
        if extractor is None:
            print("QWEN_API_KEY is not set", file=sys.stderr)
            return 1
        return read_slip(extractor, args.image, args.save)
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

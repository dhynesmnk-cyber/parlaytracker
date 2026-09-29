"""Maintenance commands: `python -m parlaytracker.cli --help` (SPEC.md sections 8.3 and 13).

    export-sample <id> <path>   write a saved raw response out as a test fixture
    backfill                    settle everything logged before the worker could (Phase 4)
    read-slip <image> [--save]  read a slip screenshot with Qwen; --save keeps the raw reply
    import-slips <csv> --user <email> [--apply] [--include-doubtful]
                                load transcribed slips; a dry run unless --apply
    check-import <csv>          after backfill: do our results match the slips'?
    export-recording <event> <dir>  write a recorded game (RECORD_EVENT_IDS) out for replay
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy import select
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
    if finals.skipped:
        # Settling now would flag a game "not final 8 hours after its start" that ESPN simply
        # couldn't be asked about. Nothing has been settled or flagged: run it again.
        print(f"{finals.skipped} game(s) not reachable on ESPN: nothing settled. Run the "
              "backfill again in a few minutes.", file=sys.stderr)
        return 1
    settled = Settle(engine, router)()
    verified = VerifyNfl(engine, nflverse)()
    print(f"games not yet reachable: {finals.skipped}")
    print(f"settled from ESPN: {settled.settled}, flagged for Review: {settled.flagged}")
    print(f"NFL: settled from nflverse {verified.settled}, verified {verified.verified}, "
          f"flagged {verified.flagged}, left unverified {verified.skipped}")
    return 0


def export_recording(engine: Engine, espn_event_id: str, directory: Path) -> int:
    """Write every recording of one game, in the order it was saved, as `NNNN_source_kind.json`
    plus an `index.json`: a real game to replay through `poll_nfl_live` (section 11)."""
    with Session(engine) as session:
        samples = session.scalars(
            select(RawSample).where(RawSample.reason == "recording",
                                    RawSample.espn_event_id == espn_event_id)
            .order_by(RawSample.id)).all()
        if not samples:
            print(f"no recordings for event {espn_event_id}: set RECORD_EVENT_IDS before the "
                  "game and restart the worker", file=sys.stderr)
            return 1
        directory.mkdir(parents=True, exist_ok=True)
        index = []
        for n, sample in enumerate(samples, start=1):
            kind = "scoreboard" if sample.url.rstrip("/").endswith("scoreboard") else "summary"
            name = f"{n:04d}_{sample.source}_{kind}.json"
            (directory / name).write_text(sample.body)
            index.append({"file": name, "source": sample.source, "kind": kind,
                          "url": sample.url, "fetched_at": sample.fetched_at.isoformat()})
        (directory / "index.json").write_text(json.dumps(index, indent=2))
        print(f"wrote {len(samples)} responses to {directory}")
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


def import_slips(engine: Engine, path: Path, user: str, apply: bool, include_doubtful: bool,
                 games_for=None, roster_for=None) -> int:
    """Plan the CSV's slips against ESPN and, with `apply`, write the ones that are sound."""
    from parlaytracker.ingest import espn, slip_import

    wait = 30.0  # a run of requests may have to wait for ESPN's one-per-2-seconds slots
    games_for = games_for or (lambda sport, day: espn.fetch_scoreboard(sport, day, wait).games)
    roster_for = roster_for or (lambda sport, team: espn.fetch_roster(sport, team, wait))
    try:
        slips = slip_import.parse_csv(path.read_text())
    except (OSError, slip_import.CsvError) as e:
        print(f"can't read {path}: {e}", file=sys.stderr)
        return 1
    with Session(engine) as session:
        plans = slip_import.plan_import(session, slips, games_for=games_for,
                                        roster_for=roster_for)
        print(slip_import.summarize(plans))
        if not apply:
            print("\ndry run: nothing was written. Add --apply to import the ok slips"
                  + (" (and, with --include-doubtful, the doubtful ones)." if not include_doubtful
                     else "."))
            return 0
        report = slip_import.apply_plans(session, plans, user, include_doubtful=include_doubtful)
        session.commit()
    print(f"\nimported {len(report.imported)} slip(s); skipped "
          f"{len(report.skipped)}: {report.skipped or 'none'}")
    return 0


def check_import(engine: Engine, path: Path) -> int:
    from parlaytracker.ingest import slip_import

    try:
        slips = slip_import.parse_csv(path.read_text())
    except (OSError, slip_import.CsvError) as e:
        print(f"can't read {path}: {e}", file=sys.stderr)
        return 1
    with Session(engine) as session:
        report = slip_import.check_import(session, slips)
    print(f"{report.slips} slips in the database; legs: {report.legs_agree} agree, "
          f"{report.legs_pending} not settled yet, {report.legs_review} in Review; "
          f"slips agreeing: {report.slips_agree}")
    if report.not_imported:
        print(f"not imported: {', '.join(report.not_imported)}")
    for m in report.mismatches:
        print(f"MISMATCH {m.slip_id}: {m.what}")
    for note in report.payout_notes:
        print(f"payout: {note}")
    return 1 if report.mismatches else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parlaytracker.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-sample", help="write a raw sample to a file")
    export.add_argument("id", type=int, help="raw_samples.id")
    export.add_argument("path", type=Path)
    commands.add_parser("backfill", help="settle everything logged since Phase 2")
    recording = commands.add_parser("export-recording", help="write a recorded game out")
    recording.add_argument("event", help="ESPN event id")
    recording.add_argument("directory", type=Path)
    read = commands.add_parser("read-slip", help="read a slip screenshot with Qwen")
    read.add_argument("image", type=Path)
    read.add_argument("--save", type=Path, help="write the raw model reply to this file")
    imp = commands.add_parser("import-slips", help="load transcribed slips from a CSV")
    imp.add_argument("csv", type=Path)
    imp.add_argument("--user", required=True, help="the login the slips are logged under")
    imp.add_argument("--apply", action="store_true", help="write them (default: dry run)")
    imp.add_argument("--include-doubtful", action="store_true")
    chk = commands.add_parser("check-import", help="compare settled results with a CSV")
    chk.add_argument("csv", type=Path)
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
    if args.command == "export-recording":
        return export_recording(engine, args.event, args.directory)
    if args.command == "check-import":
        return check_import(engine, args.csv)
    if args.command == "import-slips":
        return import_slips(engine, args.csv, args.user, args.apply, args.include_doubtful)

    lock_conn = engine.connect()  # the worker's lock: two writers would poll ESPN twice
    if not try_lock(lock_conn):
        print("the worker is running: stop it first (`deploy/compose.sh stop worker`), run the "
              "backfill, then start it again", file=sys.stderr)
        return 1
    breakers = Breakers(engine)
    breakers.load()
    router = EspnRouter(breakers, sample_sink=sample_sink(engine), default_max_wait=30.0)
    try:
        return backfill(engine, router, lambda: NflverseData(breakers))
    finally:
        breakers.persist()
        lock_conn.close()


if __name__ == "__main__":
    sys.exit(main())

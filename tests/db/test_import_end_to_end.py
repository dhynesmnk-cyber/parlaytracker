"""Importing transcribed slips end to end: written through `services`, settled by the same jobs
as any slip, then compared with what the sportsbook said (ARI @ SF, recorded)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from support import (
    ARI_SF,
    FINAL_AT,
    FakeRouter,
    ari_sf_box,
    load,
)

from parlaytracker import cli
from parlaytracker.core.models import (
    Event,
    EventStatus,
    LegResult,
    MarketType,
    Slip,
    SlipStatus,
    SlipType,
    Sport,
    Sportsbook,
)
from parlaytracker.ingest import espn, slip_import
from parlaytracker.worker.settle import CheckFinals, Settle
from import_support import HEADER, ROSTERS, csv_text, row, two_legs

pytestmark = pytest.mark.db

BOARD = espn.parse_scoreboard(Sport.NFL, load("espn", "nfl_scoreboard_2026-09-27_final.json"))
WEEKS_LATER = FINAL_AT + timedelta(days=20)
USER = "user1@example.com"


@pytest.fixture(autouse=True)
def no_leftover_hard_rock(engine):
    """`clean` doesn't truncate sportsbooks, and these tests add (or expect to add) one."""
    def remove():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM sportsbooks WHERE name LIKE 'Hard Rock%'"))
    remove()
    yield
    remove()


def games_for(sport, day):
    return BOARD.games if day.isoformat() == "2026-09-27" else []


def roster_for(sport, team_id):
    return ROSTERS[team_id]


def planned(session, rows):
    return slip_import.plan_import(session, slip_import.parse_csv(csv_text(rows)),
                                   games_for=games_for, roster_for=roster_for)


def test_apply_writes_the_slip_through_the_services_with_its_original_date(engine, clean):
    rows = two_legs(boost_pct="33", placed_at_iso="2026-09-27T12:00:00-04:00")
    with Session(engine) as s:
        plans = planned(s, rows)
        assert any("will be added" in n for n in plans[0].notes)
        report = slip_import.apply_plans(s, plans, USER)
        s.commit()
    assert report.imported == ["111"] and report.skipped == {}
    with Session(engine) as s:
        slip = s.scalars(select(Slip)).one()
        assert (slip.slip_type, slip.american_odds, slip.stake, slip.boosted, slip.is_placed) == (
            SlipType.SGP, 450, D("40.00"), True, True)
        assert slip.logged_by == USER and slip.notes == "import Hard Rock #111"
        assert slip.source.value == "screenshot"
        assert slip.created_at == datetime(2026, 9, 27, 16, 0, tzinfo=UTC)  # 12:00 ET
        assert (slip.status, slip.payout) == (SlipStatus.PENDING, None)
        assert [(leg.market_type, leg.espn_athlete_id, leg.player_name, leg.line,
                 leg.american_odds) for leg in slip.legs] == [
            (MarketType.PLAYER_RECEPTIONS, "4361307", "Trey McBride", D("9.5"), None),
            (MarketType.PLAYER_TOUCHDOWNS, "3040151", "George Kittle", D("0.5"), None)]
        book = s.get(Sportsbook, slip.sportsbook_id)
        assert (book.name, book.odds_api_key) == ("Hard Rock", "hardrockbet")
        event = s.scalars(select(Event)).one()
        # Not "final": the worker finds a finished game final and dates it, so the ten-minute
        # gate holds (section 7.1).
        assert event.status is EventStatus.SCHEDULED and event.espn_event_id == ARI_SF


def test_importing_twice_adds_nothing(engine, clean):
    for _ in range(2):
        with Session(engine) as s:
            plans = planned(s, two_legs())
            report = slip_import.apply_plans(s, plans, USER)
            s.commit()
    assert report.skipped == {"111": "already imported"} and report.imported == []
    with Session(engine) as s:
        assert len(s.scalars(select(Slip)).all()) == 1
        assert len(s.scalars(select(Sportsbook).where(Sportsbook.name == "Hard Rock")).all()) == 1


def test_an_existing_sportsbook_is_reused_by_its_printed_name(engine, clean):
    with Session(engine) as s:
        s.add(Sportsbook(name="Hard Rock Bet", odds_api_key="hardrockbet"))
        s.commit()
    with Session(engine) as s:
        slip_import.apply_plans(s, planned(s, two_legs()), USER)
        s.commit()
        assert len(s.scalars(select(Sportsbook).where(Sportsbook.name.like("Hard Rock%")))
                   .all()) == 1


def test_errors_and_doubts_are_skipped_unless_doubts_are_allowed(engine, clean):
    rows = (two_legs(slip_id="ok") + two_legs(slip_id="bad", matchup="Nobody vs Nobody")
            + two_legs(slip_id="doubt", placed_at_iso="2026-09-27T17:00:00-04:00"))
    with Session(engine) as s:
        report = slip_import.apply_plans(s, planned(s, rows), USER)
        s.commit()
    assert report.imported == ["ok"]
    assert report.skipped == {"bad": "error", "doubt": "doubtful"}
    with Session(engine) as s:
        report = slip_import.apply_plans(s, planned(s, rows), USER, include_doubtful=True)
        s.commit()
        assert report.imported == ["doubt"]
        assert report.skipped == {"ok": "already imported", "bad": "error"}


def test_one_slip_the_database_rejects_does_not_stop_the_rest(engine, clean):
    dup = two_legs(slip_id="dup")
    dup[1].update(player="Trey McBride", market="receptions", line="9.5",
                  raw_text="TREY MCBRIDE - RECEPTIONS")  # the same selection twice
    with Session(engine) as s:
        report = slip_import.apply_plans(s, planned(s, dup + two_legs(slip_id="fine")), USER)
        s.commit()
    assert report.imported == ["fine"]
    assert report.skipped["dup"].startswith("rejected:")


def settle_all(engine):
    router = FakeRouter()
    router.boards[(Sport.NFL, datetime(2026, 9, 27).date())] = BOARD
    router.boxes[ARI_SF] = ari_sf_box()
    CheckFinals(engine, router)(WEEKS_LATER, force=True, backfill=True)
    Settle(engine, router)(WEEKS_LATER)


def sportsbook_rows():
    # McBride 9 receptions, Kittle 2 TDs, Brissett 38 completions, Ryland 3 FGs (ESPN's box)
    return [
        row(slip_id="a", leg_seq="1", player="Trey McBride", market="receptions", line="8.5",
            result="won", raw_text="TREY MCBRIDE - RECEPTIONS", leg_count="2", status="won",
            paid="220.10"),
        row(slip_id="a", leg_seq="2", player="George Kittle", market="touchdowns", line="1.5",
            result="won", raw_text="GEORGE KITTLE - TO SCORE 2+ TDS", leg_count="2",
            status="won", paid="220.10"),
        row(slip_id="b", leg_seq="1", player="Jacoby Brissett", market="pass_completions",
            line="38.5", result="lost", raw_text="JACOBY BRISSETT - PASS COMPLETIONS",
            leg_count="2"),
        row(slip_id="b", leg_seq="2", player="Chad Ryland", market="field_goals_made",
            line="2.5", result="won", raw_text="CHAD RYLAND - FIELD GOALS MADE", leg_count="2"),
    ]


def test_settled_results_are_compared_with_what_the_sportsbook_said(engine, clean):
    rows = sportsbook_rows()
    with Session(engine) as s:
        slip_import.apply_plans(s, planned(s, rows), USER)
        s.commit()
    settle_all(engine)
    with Session(engine) as s:
        report = slip_import.check_import(s, slip_import.parse_csv(csv_text(rows)))
    assert (report.slips, report.legs_agree, report.slips_agree) == (2, 4, 2)
    assert report.mismatches == [] and report.not_imported == []
    # 40 at +450 pays 220.00; the sportsbook paid 220.10: a note, not a mismatch.
    assert report.payout_notes == ["a: paid 220.10, computed 220.00"]


def test_a_result_that_differs_from_the_sportsbooks_is_reported_with_the_value(engine, clean):
    rows = sportsbook_rows()
    rows[0]["line"] = "9.5"  # the sportsbook said won, but McBride had 9 receptions
    with Session(engine) as s:
        slip_import.apply_plans(s, planned(s, rows), USER, include_doubtful=True)
        s.commit()
    settle_all(engine)
    with Session(engine) as s:
        report = slip_import.check_import(s, slip_import.parse_csv(csv_text(rows)))
    assert [m.slip_id for m in report.mismatches] == ["a", "a"]
    assert report.mismatches[0].what == (
        "leg 1 Trey McBride receptions over 9.5: the slip says won, we settled loss at 9")
    assert report.mismatches[1].what == "the slip says won, we have loss"
    assert report.legs_agree == 3


def test_legs_not_yet_settled_are_counted_not_reported(engine, clean):
    rows = sportsbook_rows()
    with Session(engine) as s:
        slip_import.apply_plans(s, planned(s, rows), USER)
        s.commit()
        report = slip_import.check_import(s, slip_import.parse_csv(csv_text(rows)))
    assert (report.legs_pending, report.legs_agree, report.mismatches) == (4, 0, [])


def test_a_slip_that_was_never_imported_is_listed(engine, clean):
    with Session(engine) as s:
        report = slip_import.check_import(s, slip_import.parse_csv(csv_text(two_legs())))
    assert report.not_imported == ["111"]


# --- the commands -----------------------------------------------------------------------------


def write_csv(tmp_path, rows):
    path = tmp_path / "slips.csv"
    path.write_text(csv_text(rows))
    return path


def test_the_command_is_a_dry_run_unless_told_to_apply(engine, clean, tmp_path, capsys):
    path = write_csv(tmp_path, two_legs())
    assert cli.import_slips(engine, path, USER, False, False, games_for, roster_for) == 0
    out = capsys.readouterr().out
    assert "OK        111" in out and "nothing was written" in out
    with Session(engine) as s:
        assert s.scalars(select(Slip)).all() == []
    assert cli.import_slips(engine, path, USER, True, False, games_for, roster_for) == 0
    assert "imported 1 slip(s)" in capsys.readouterr().out
    with Session(engine) as s:
        assert len(s.scalars(select(Slip)).all()) == 1


def test_a_file_that_cannot_be_read_changes_nothing(engine, clean, tmp_path, capsys):
    path = tmp_path / "bad.csv"
    path.write_text(HEADER.replace("wager", "stake") + "\n")
    assert cli.import_slips(engine, path, USER, True, False, games_for, roster_for) == 1
    assert "missing column(s): wager" in capsys.readouterr().err
    assert cli.import_slips(engine, tmp_path / "nope.csv", USER, True, False,
                            games_for, roster_for) == 1


def test_check_import_exits_nonzero_on_a_mismatch(engine, clean, tmp_path, capsys):
    rows = sportsbook_rows()
    rows[0]["line"] = "9.5"
    path = write_csv(tmp_path, rows)
    cli.import_slips(engine, path, USER, True, True, games_for, roster_for)
    settle_all(engine)
    assert cli.check_import(engine, path) == 1
    assert "MISMATCH a: leg 1 Trey McBride" in capsys.readouterr().out
    good = write_csv(tmp_path, sportsbook_rows())
    assert cli.check_import(engine, good) == 1  # the wrong-line slip is still what's stored


def test_backfill_stops_and_says_so_when_a_game_cannot_be_reached(engine, clean, capsys):
    from support import nflverse_loader

    from parlaytracker.ingest.nflverse import NflverseData
    from parlaytracker.ingest.router import Breakers

    with Session(engine) as s:
        slip_import.apply_plans(s, planned(s, two_legs()), USER)
        s.commit()
    router = FakeRouter()  # nothing registered: ESPN is down for every request
    code = cli.backfill(engine, router, lambda: NflverseData(Breakers(engine=None),
                                                             nflverse_loader()))
    assert code == 1
    assert "not reachable on ESPN: nothing settled" in capsys.readouterr().err
    with Session(engine) as s:
        legs = s.scalars(select(Slip)).one().legs
        assert all(leg.result is LegResult.PENDING and not leg.needs_review for leg in legs)

"""canary, prune_samples, the raw-sample sink, and the maintenance CLI (SPEC.md 8.1, 8.3, 13)."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from support import (
    ARI_SF,
    DAY_AFTER,
    KICKOFF,
    FakeRouter,
    add_event,
    add_slip,
    ari_sf_box,
    commit,
    legs_of,
    load,
    nflverse_loader,
    total,
)

from parlaytracker import cli
from parlaytracker.core.models import DataSource, HealthState, LegResult, RawSample, Sport
from parlaytracker.ingest import espn
from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.router import AllProvidersFailed, Breakers, SampleRecord
from parlaytracker.worker.__main__ import try_lock
from parlaytracker.worker.settle import Canary, CheckFinals, Settle, prune_samples, sample_sink

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)  # 10:00 ET Monday: Sunday's games are latest
SUNDAY_BOARD = espn.parse_scoreboard(
    Sport.NFL, load("espn", "nfl_scoreboard_2026-09-27_final.json"))


def nflverse(breakers=None, patch=None):
    breakers = breakers or Breakers(engine=None)
    return lambda: NflverseData(breakers, nflverse_loader(patch))


# --- canary ---------------------------------------------------------------------------------


def canary_router() -> FakeRouter:
    router = FakeRouter()
    router.boards[(Sport.NFL, NOW.date())] = espn.ScoreboardResult([], {})  # nothing today yet
    router.boards[(Sport.NFL, (NOW - timedelta(days=1)).date())] = SUNDAY_BOARD
    final = SUNDAY_BOARD.games[-1]
    router.boxes[final.espn_event_id] = ari_sf_box()
    return router


def test_the_canary_parses_the_latest_completed_game_from_every_provider():
    router = canary_router()
    report = Canary(router, nflverse())(NOW)
    assert report == {"espn_web": "ok", "espn_site": "ok", "espn_cdn": "ok", "nflverse": "ok"}
    asked = [(c[2]) for c in router.calls_of("box")]
    assert asked == [DataSource.ESPN_WEB, DataSource.ESPN_SITE, DataSource.ESPN_CDN]


def test_a_provider_that_fails_the_canary_is_named_and_the_rest_still_run():
    router = canary_router()
    original = router.box_score

    def cdn_broken(sport, event_id, max_wait=0.0, *, only=None):
        if only is DataSource.ESPN_CDN:
            raise AllProvidersFailed({"espn_cdn": "schema: box score: changed"})
        return original(sport, event_id, max_wait, only=only)

    router.box_score = cdn_broken
    report = Canary(router, nflverse())(NOW)
    assert report["espn_web"] == "ok" and report["espn_site"] == "ok"
    assert "changed" in report["espn_cdn"] and report["nflverse"] == "ok"


def test_the_canary_reports_nflverse_failing_and_opens_its_breaker():
    breakers = Breakers(engine=None)

    def renamed(frames):
        frames["players"] = frames["players"].rename({"espn_id": "espn"})

    report = Canary(canary_router(), nflverse(breakers, renamed))(NOW)
    assert "missing columns" in report["nflverse"]
    assert breakers["nflverse"].state is HealthState.OPEN


def test_the_canary_in_the_offseason_only_checks_nflverse():
    router = FakeRouter()  # no boards registered: every scoreboard fails
    report = Canary(router, nflverse())(NOW)
    assert report == {"nflverse": "ok"} and router.calls_of("box") == []


# --- prune_samples --------------------------------------------------------------------------


def add_sample(session, source, reason, when, n=0):
    session.add(RawSample(source=source, reason=reason, url=f"u{n}", body=f"b{n}",
                          fetched_at=when))


def test_prune_keeps_the_last_five_failures_per_provider(engine, clean):
    def build(s):
        for n in range(8):
            add_sample(s, "espn_web", "failure", NOW - timedelta(hours=n), n)
        for n in range(3):
            add_sample(s, "espn_site", "failure", NOW - timedelta(hours=n), n)

    commit(engine, build)
    assert prune_samples(engine, NOW) == 3
    with Session(engine) as s:
        web = s.scalars(select(RawSample).where(RawSample.source == "espn_web")
                        .order_by(RawSample.fetched_at.desc())).all()
        assert [r.url for r in web] == ["u0", "u1", "u2", "u3", "u4"]  # the newest five
        assert s.query(RawSample).filter_by(source="espn_site").count() == 3


def test_prune_deletes_recordings_older_than_thirty_days_only(engine, clean):
    def build(s):
        add_sample(s, "espn_web", "recording", NOW - timedelta(days=30, seconds=1), 1)
        add_sample(s, "espn_web", "recording", NOW - timedelta(days=29), 2)
        add_sample(s, "espn_web", "failure", NOW - timedelta(days=90), 3)  # kept: under 5

    commit(engine, build)
    assert prune_samples(engine, NOW) == 1
    with Session(engine) as s:
        assert sorted(r.url for r in s.scalars(select(RawSample))) == ["u2", "u3"]


def test_prune_with_nothing_to_do_is_quiet(engine, clean):
    assert prune_samples(engine, NOW) == 0


# --- the sample sink and export-sample ------------------------------------------------------


def test_the_sink_stores_what_the_router_hands_it(engine, clean):
    sample_sink(engine)(SampleRecord("espn_web", "failure", "https://x", 200, ARI_SF,
                                     "schema: bad", '{"a": 1}'))
    with Session(engine) as s:
        row = s.scalars(select(RawSample)).one()
    assert (row.source, row.reason, row.espn_event_id, row.error, row.body) == (
        "espn_web", "failure", ARI_SF, "schema: bad", '{"a": 1}')


def test_export_sample_writes_the_body_to_a_fixture_file(engine, clean, tmp_path, capsys):
    sample_sink(engine)(SampleRecord("espn_web", "recording", "u", None, ARI_SF, None, '{"ok":1}'))
    with Session(engine) as s:
        sample_id = s.scalars(select(RawSample.id)).one()
    target = tmp_path / "fixture.json"
    assert cli.export_sample(engine, sample_id, target) == 0
    assert target.read_text() == '{"ok":1}' and "wrote 8 bytes" in capsys.readouterr().out


def test_export_sample_says_so_when_the_id_is_unknown(engine, clean, tmp_path, capsys):
    assert cli.export_sample(engine, 9999, tmp_path / "x.json") == 1
    assert "no raw sample with id 9999" in capsys.readouterr().err
    assert not (tmp_path / "x.json").exists()


# --- backfill -------------------------------------------------------------------------------


def test_backfill_settles_games_logged_before_the_worker_could(engine, clean, capsys):
    router = FakeRouter()
    router.boards[(Sport.NFL, KICKOFF.date())] = SUNDAY_BOARD
    router.boxes[ARI_SF] = ari_sf_box()
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    breakers = Breakers(engine=None)
    weeks_later = DAY_AFTER + timedelta(days=20)
    # the CLI calls the jobs with the real clock; run them on the fake one here
    CheckFinals(engine, router)(weeks_later, force=True, backfill=True)
    Settle(engine, router)(weeks_later)
    leg = legs_of(engine)[0]
    assert leg.result is LegResult.WIN and leg.settlement_source is DataSource.ESPN_WEB
    assert breakers["nflverse"].allow()
    assert cli.backfill(engine, router, nflverse()) == 0  # a second run changes nothing
    assert "settled from ESPN: 0" in capsys.readouterr().out
    assert legs_of(engine)[0].settled_at == weeks_later


def test_backfill_refuses_to_run_beside_a_live_worker(engine, clean, monkeypatch, capsys):
    with engine.connect() as worker:
        assert try_lock(worker)
        monkeypatch.setattr(cli, "make_engine", lambda: engine)
        try:
            assert cli.main(["backfill"]) == 1
        finally:
            worker.execute(text("SELECT pg_advisory_unlock_all()"))
            worker.commit()
    assert "stop it first" in capsys.readouterr().err


# --- read-slip ------------------------------------------------------------------------------


class _Reader:
    def __init__(self, reply="{}", error=None):
        self.last_reply, self._error = reply, error

    def extract(self, image):
        from parlaytracker.ingest.extraction import parse_reply

        if self._error:
            raise self._error
        return parse_reply(self.last_reply)


def _png(tmp_path):
    import io

    from PIL import Image
    path = tmp_path / "slip.png"
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buffer, "PNG")
    path.write_bytes(buffer.getvalue())
    return path


def test_read_slip_prints_what_was_read_and_saves_the_raw_reply(tmp_path, capsys):
    reply = '{"sportsbook_text": "DraftKings", "american_odds": -110}'
    target = tmp_path / "real_01.json"
    assert cli.read_slip(_Reader(reply), _png(tmp_path), target) == 0
    out = capsys.readouterr().out
    assert '"sportsbook_text": "DraftKings"' in out and "check it for anything private" in out
    assert target.read_text() == reply  # the raw reply, not a re-serialisation


def test_read_slip_without_save_writes_nothing(tmp_path, capsys):
    assert cli.read_slip(_Reader('{"american_odds": 150}'), _png(tmp_path), None) == 0
    assert list(tmp_path.glob("*.json")) == []


def test_read_slip_reports_a_bad_file_or_a_failed_read(tmp_path, capsys):
    from parlaytracker.ingest.extraction import ExtractionError
    assert cli.read_slip(_Reader(), tmp_path / "missing.png", None) == 1
    (tmp_path / "text.png").write_text("not an image")
    assert cli.read_slip(_Reader(), tmp_path / "text.png", None) == 1
    assert cli.read_slip(_Reader(error=ExtractionError("no")), _png(tmp_path), None) == 1
    assert capsys.readouterr().err.count("couldn't read") == 3


def test_read_slip_needs_a_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("DISPLAY_TZ", "Europe/London")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    from parlaytracker.core.config import get_settings
    get_settings.cache_clear()
    try:
        assert cli.main(["read-slip", str(_png(tmp_path))]) == 1
    finally:
        get_settings.cache_clear()
    assert "QWEN_API_KEY is not set" in capsys.readouterr().err

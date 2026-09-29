"""Recording a game, exporting it, and replaying it (SPEC.md sections 8.3 and 11).

`RECORD_EVENT_IDS` saves every response for a watched game; `cli export-recording` writes them
out; a replay through `poll_nfl_live` must end where the original run did. This is the loop a
real recorded game will go through, exercised here on a synthetic timeline of real documents.
"""
import json
from datetime import timedelta

import respx
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from support import (
    ARI_SF,
    KICKOFF,
    MCBRIDE,
    RecordingRouter,
    SimClock,
    add_event,
    add_slip,
    commit,
    event_of,
    legs_of,
    receptions,
    team_total,
    total,
)
from test_live_simulation import Espn

from parlaytracker import cli
from parlaytracker.core.models import Event, EventStatus as S, Leg, RawSample
from parlaytracker.ingest.http import RateLimiter
from parlaytracker.ingest.router import Breakers, EspnRouter
from parlaytracker.worker.live import PollNflLive
from parlaytracker.worker.settle import sample_sink

import pytest

pytestmark = pytest.mark.db

TICKS = 40
OTHER = "401872954"  # a second game nobody asked to record


def moment(n: int):
    """The game at tick n: the clock runs, the score creeps up, McBride catches a pass."""
    return (S.IN_PROGRESS, 2, 900 - n * 20, n // 4, n // 5, n // 8)


def run_recorded(engine, tmp_path):
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    espn = Espn(clock)
    breakers = Breakers(engine, clock=lambda: clock.now)
    router = EspnRouter(breakers, limiter=RateLimiter(2.0, 20, clock=clock.monotonic,
                                                      sleep=clock.sleep),
                        sample_sink=sample_sink(engine), record_event_ids={ARI_SF})
    job = PollNflLive(engine, router, sleep=clock.sleep)
    calls = 0
    with respx.mock(assert_all_called=False) as mock:
        espn.install(mock)
        for n in range(TICKS):
            espn.show(*moment(n))
            if 10 <= n < 16:
                espn.block("web")        # web goes down for three minutes: site answers
            else:
                espn.unblock("web")
            out = job(clock.now)
            calls += out.scoreboards + out.boxes
            clock.now += timedelta(seconds=30)
    return calls


def state(engine):
    e = event_of(engine)
    return (e.status, e.period, e.clock_seconds, e.home_score, e.away_score,
            [(leg.live_value, leg.live_source) for leg in legs_of(engine)])


def test_a_recorded_game_replays_to_the_same_state(engine, clean, tmp_path):
    def build(s):
        add_slip(s, add_event(s), [total("65.5"), team_total("home", "35.5"),
                                   receptions(MCBRIDE, "8.5")])
        add_slip(s, add_event(s, espn_id=OTHER), [total("50.5")])

    commit(engine, build)
    calls = run_recorded(engine, tmp_path)
    original = state(engine)
    assert original[0] is S.IN_PROGRESS and original[3] == 9   # it got somewhere: 9 after 39 ticks

    # every request that succeeded for the watched game was recorded, and only that game's
    with Session(engine) as s:
        recordings = s.scalars(select(RawSample).where(RawSample.reason == "recording")).all()
    assert len(recordings) == calls
    assert {r.espn_event_id for r in recordings} == {ARI_SF}
    assert {r.source for r in recordings} == {"espn_web", "espn_site"}   # the outage is in it

    # export: the files, in order, and an index
    out = tmp_path / "game"
    assert cli.export_recording(engine, ARI_SF, out) == 0
    index = json.loads((out / "index.json").read_text())
    assert len(index) == calls
    assert [e["file"][:4] for e in index] == [f"{n:04d}" for n in range(1, calls + 1)]
    assert {e["kind"] for e in index} == {"scoreboard", "summary"}
    assert all((out / e["file"]).exists() for e in index)

    # wipe the game's state, then replay the export through a fresh job on the same schedule
    with Session(engine) as s:
        s.execute(update(Event).values(
            status=S.SCHEDULED, period=None, clock_seconds=None, home_score=None,
            away_score=None, last_polled_at=None, last_progress_at=None, final_at=None))
        s.execute(update(Leg).values(live_value=None, live_updated_at=None, live_source=None))
        s.commit()
    assert state(engine)[:5] == (S.SCHEDULED, None, None, None, None)
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    replay = RecordingRouter(out)
    job = PollNflLive(engine, replay, sleep=clock.sleep)
    for _ in range(TICKS):
        job(clock.now)
        clock.now += timedelta(seconds=30)
    assert len(replay.calls) == calls
    assert state(engine) == original


def test_exporting_a_game_that_was_not_recorded_says_how_to_record_it(engine, clean, tmp_path,
                                                                     capsys):
    assert cli.export_recording(engine, "999", tmp_path / "nothing") == 1
    assert "RECORD_EVENT_IDS" in capsys.readouterr().err
    assert not (tmp_path / "nothing").exists()


def test_a_scoreboard_recording_is_tagged_with_the_watched_event(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    run_recorded(engine, None)
    with Session(engine) as s:
        boards = [r for r in s.scalars(select(RawSample).where(RawSample.reason == "recording"))
                  if r.url.endswith("scoreboard")]
    assert boards and all(r.espn_event_id == ARI_SF for r in boards)
    assert any(OTHER in r.body for r in boards)   # the 14-game body holds other games too

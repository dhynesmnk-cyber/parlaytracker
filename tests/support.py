"""Shared helpers for the worker's database tests: committed data, a fake ESPN router that
answers with the real parsers on recorded fixtures, and an nflverse loader over a recorded
slice."""
import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from parlaytracker.core import services as svc
from parlaytracker.core.models import DataSource, Event, EventStatus, Leg, Sport, Sportsbook
from parlaytracker.core.schemas import SlipIn
from parlaytracker.ingest import espn
from parlaytracker.ingest.router import AllProvidersFailed, Routed

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ARI @ SF, week 3: final 30-36, 2026-09-27 4:05 pm ET.
ARI_SF = "401872958"
KICKOFF = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)
FINAL_AT = datetime(2026, 9, 27, 23, 30, tzinfo=UTC)
DAY_AFTER = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)  # 11:00 ET Monday: after 10:00 ET verify

# ESPN athlete ids in that game (see the fixtures)
MCBRIDE = "4361307"      # ARI: 9 receptions, 75 receiving yards
WILSON = "4360761"       # ARI: 11 receptions, 89 yards
BROOKS = "4692835"       # ARI: targeted, 0 receptions
BRISSETT = "2578570"     # ARI QB: no receiving line in ESPN; nflverse says 0 receptions
SEUMALO = "2978247"      # SF OL: 87 snaps, no nflverse stat line
JAYDEN_WILLIAMS = "4709718"  # 4 snaps in nflverse; tests set them to 0


def load(kind: str, name: str):
    return json.loads((FIXTURES / kind / name).read_text())


def commit(engine: Engine, build):
    with Session(engine, expire_on_commit=False) as s:
        result = build(s)
        s.commit()
        return result


def add_event(session: Session, *, espn_id: str = ARI_SF, sport: Sport = Sport.NFL,
              start: datetime = KICKOFF, status: EventStatus = EventStatus.SCHEDULED,
              home: str = "San Francisco 49ers", away: str = "Arizona Cardinals",
              final_at: datetime | None = None, scores: tuple[int, int] | None = None,
              period: int | None = None, clock: int | None = None) -> Event:
    event = Event(sport=sport, espn_event_id=espn_id, home_team=home, away_team=away,
                  home_espn_team_id="25", away_espn_team_id="22", start_time=start,
                  status=status, final_at=final_at, period=period, clock_seconds=clock)
    if scores:
        event.home_score, event.away_score = scores
    session.add(event)
    session.flush()
    return event


def add_slip(session: Session, event: Event, legs: list[dict], book: str = "DraftKings",
             **overrides):
    """One leg is a single; several on one game are a same-game parlay."""
    single = len(legs) == 1
    book_id = session.scalars(select(Sportsbook).where(Sportsbook.name == book)).one().id
    data = {"is_placed": True, "stake": "10.00", "slip_type": "single" if single else "sgp",
            "sportsbook_id": book_id, "american_odds": -110, "source": "quick_add",
            "legs": [{"event_id": event.id, "american_odds": -110 if single else None, **leg}
                     for leg in legs], **overrides}
    return svc.create_slip(session, SlipIn(**data), "a@example.com")


def total(line: str = "65.5") -> dict:
    return {"market_type": "game_total", "line": line}


def team_total(side: str, line: str) -> dict:
    return {"market_type": "team_total", "side": side, "line": line}


def spread(side: str, line: str) -> dict:
    return {"market_type": "alt_spread", "side": side, "line": line}


def player(market: str, athlete: str, line: str) -> dict:
    return {"market_type": market, "espn_athlete_id": athlete, "line": line}


def receptions(athlete: str, line: str) -> dict:
    return player("player_receptions", athlete, line)


def legs_of(engine: Engine) -> list[Leg]:
    with Session(engine) as s:
        return list(s.scalars(select(Leg).order_by(Leg.id)))


def event_of(engine: Engine, espn_id: str = ARI_SF) -> Event:
    with Session(engine) as s:
        return s.scalars(select(Event).where(Event.espn_event_id == espn_id)).one()


class FakeRouter:
    """Answers `scoreboard` and `box_score` from what a test registers; anything else fails
    the way the real router does when every provider is down."""

    def __init__(self, provider: DataSource = DataSource.ESPN_WEB):
        self.provider = provider
        self.boards: dict[tuple[Sport, date], espn.ScoreboardResult | Exception] = {}
        self.boxes: dict[str, espn.BoxScore | Exception] = {}
        self.calls: list[tuple] = []

    def scoreboard(self, sport, day, max_wait=0.0, **_):
        self.calls.append(("board", sport, day))
        return self._answer(self.boards.get((sport, day)), f"scoreboard {sport} {day}")

    def box_score(self, sport, espn_event_id, max_wait=0.0, *, only=None):
        self.calls.append(("box", espn_event_id, only))
        return self._answer(self.boxes.get(espn_event_id), f"box score {espn_event_id}")

    def _answer(self, value, what):
        if value is None:
            raise AllProvidersFailed({"espn_web": f"no {what} registered"})
        if isinstance(value, Exception):
            raise value
        return Routed(self.provider, value)

    def calls_of(self, kind: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == kind]


def ari_sf_box() -> espn.BoxScore:
    return espn.parse_box_score(Sport.NFL, load("espn", "nfl_summary_401872958_final.json"))


def nba_box() -> espn.BoxScore:
    return espn.parse_box_score(Sport.NBA, load("espn", "nba_summary_401810723_final.json"))


def nflverse_loader(patch=None):
    """A loader for NflverseData over the recorded week-3 slice. `patch(frames)` may edit the
    frames first (a dict of polars DataFrames) to stage a case."""
    raw = load("nflverse", "week3_2026.json")
    frames = {name: pl.DataFrame(rows, infer_schema_length=None)
              for name, rows in {"schedules": raw["schedules"], "players": raw["players"],
                                 "player_stats": raw["player_stats"],
                                 "snap_counts": raw["snap_counts"]}.items()}
    if patch:
        patch(frames)
    return lambda dataset, season: frames[dataset]

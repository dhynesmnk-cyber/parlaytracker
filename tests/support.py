"""Shared helpers for the worker's database tests: committed data, a fake ESPN router that
answers with the real parsers on recorded fixtures, and an nflverse loader over a recorded
slice."""
import json
from datetime import UTC, date, datetime
from itertools import count
from decimal import Decimal
from pathlib import Path

import polars as pl
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from parlaytracker.core import services as svc
from parlaytracker.core.models import DataSource, Event, EventStatus, Leg, Sport, Sportsbook
from parlaytracker.core.schemas import SlipIn
from parlaytracker.ingest import espn
from parlaytracker.ingest.router import AllProvidersFailed, Routed

_LEG_IDS = count(10_000)  # ids for analytics_leg rows
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


def analytics_leg(result=None, odds=-110, line="45.5", **kw):
    """A `LegRow` for the analytics unit tests, with sensible defaults."""
    from parlaytracker.core.analytics import LegRow
    from parlaytracker.core.models import LegResult, MarketType, SlipType

    n = next(_LEG_IDS)
    start = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)
    defaults = dict(
        leg_id=n, slip_id=n, selection_key=("sel", n), logged_at=start.replace(hour=17),
        start_time=start, sport=Sport.NFL, market=MarketType.GAME_TOTAL,
        sportsbook="DraftKings", slip_type=SlipType.SINGLE, leg_count=1, is_placed=True,
        logged_by="a@example.com", line=Decimal(line), odds=odds,
        result=result or LegResult.WIN)
    return LegRow(**{**defaults, **kw})


# --- Live tracking ----------------------------------------------------------------------------


class LiveRouter:
    """A scoreboard and box-score source for `poll_nfl_live`, with a per-provider feed so a test
    can freeze one, block one, or advance both. It keeps the real router's contract: the first
    provider whose breaker is open is skipped, a failing one is recorded on the breaker, and
    `only=` asks one provider."""

    ORDER = (DataSource.ESPN_WEB, DataSource.ESPN_SITE)

    def __init__(self, breakers=None):
        from parlaytracker.ingest.router import Breakers

        self.breakers = breakers or Breakers(engine=None)
        # provider -> day -> ScoreboardResult, and provider -> event id -> BoxScore
        self.boards: dict[DataSource, dict] = {p: {} for p in self.ORDER}
        self.boxes: dict[DataSource, dict] = {p: {} for p in (*self.ORDER, DataSource.ESPN_CDN)}
        self.down: set[DataSource] = set()
        self.calls: list[tuple] = []

    def _fail(self, provider):
        from parlaytracker.core.models import FailureKind
        self.breakers.failure(provider.value, FailureKind.BLOCKED, "403")

    def scoreboard(self, sport, day, max_wait=0.0, *, only=None):
        from parlaytracker.ingest.router import ProviderOpen
        errors = {}
        for provider in ([only] if only else self.ORDER):
            try:
                self.breakers.allow(provider.value)
            except ProviderOpen as e:
                errors[provider.value] = str(e)
                continue
            self.calls.append(("board", provider, day))
            if provider in self.down:
                self._fail(provider)
                errors[provider.value] = "blocked"
                continue
            self.breakers.success(provider.value)
            return Routed(provider, self.boards[provider][day])
        raise AllProvidersFailed(errors)

    def box_score(self, sport, espn_event_id, max_wait=0.0, *, only=None):
        from parlaytracker.ingest.router import ProviderOpen
        errors = {}
        for provider in ([only] if only else list(self.boxes)):
            try:
                self.breakers.allow(provider.value)
            except ProviderOpen as e:
                errors[provider.value] = str(e)
                continue
            self.calls.append(("box", provider, espn_event_id))
            if provider in self.down:
                self._fail(provider)
                errors[provider.value] = "blocked"
                continue
            self.breakers.success(provider.value)
            return Routed(provider, self.boxes[provider][espn_event_id])
        raise AllProvidersFailed(errors)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)

    def serve(self, day, game_or_games, providers=None):
        """Both (or the given) providers answer this day's scoreboard with these games."""
        games = game_or_games if isinstance(game_or_games, list) else [game_or_games]
        for p in providers or self.ORDER:
            self.boards[p][day] = espn.ScoreboardResult(list(games), {})


def live_game(status=EventStatus.IN_PROGRESS, period=1, clock=900, home=0, away=0,
              espn_id=ARI_SF, start=KICKOFF):
    """A scoreboard game for ARI @ SF at a given moment."""
    return espn.Game(
        espn_event_id=espn_id, sport=Sport.NFL, start_time=start, status=status,
        status_detail="", period=period, clock_seconds=clock,
        home=espn.Team("25", "SF", "San Francisco 49ers"),
        away=espn.Team("22", "ARI", "Arizona Cardinals"), home_score=home, away_score=away)


# Frames for the live simulations: the real scoreboard and box-score JSON, edited to a moment
# in a game. Synthetic timelines built from real documents (not a recording of a live game).

_STATUS_TYPES = {
    EventStatus.SCHEDULED: ("STATUS_SCHEDULED", "pre", False),
    EventStatus.IN_PROGRESS: ("STATUS_IN_PROGRESS", "in", False),
    EventStatus.BREAK: ("STATUS_HALFTIME", "in", False),
    EventStatus.DELAYED: ("STATUS_DELAYED", "in", False),
    EventStatus.FINAL: ("STATUS_FINAL", "post", True),
}


def scoreboard_json(base: dict, status: EventStatus, period: int, clock: float, home: int,
                    away: int, event_id: str = ARI_SF, others: EventStatus | None = None) -> dict:
    """`base` (a real scoreboard) with `event_id` moved to a moment; `others` sets every other
    game to one status too (a whole slate in play)."""
    import copy

    payload = copy.deepcopy(base)
    for event in payload["events"]:
        mine = event["id"] == event_id
        wanted = status if mine else others
        if wanted is None:
            continue
        name, state, completed = _STATUS_TYPES[wanted]
        event["status"] = {
            "clock": float(clock), "displayClock": f"{int(clock) // 60}:{int(clock) % 60:02d}",
            "period": period,
            "type": {"id": "2", "name": name, "state": state, "completed": completed,
                     "description": name, "detail": name, "shortDetail": name}}
        for side in event["competitions"][0]["competitors"]:
            side["score"] = str(home if side["homeAway"] == "home" else away)
    return payload


def box_json(receptions: int) -> dict:
    """The real ARI @ SF box score with Trey McBride at `receptions` catches."""
    import copy

    doc = copy.deepcopy(load("espn", "nfl_summary_401872958_final.json"))
    for team in doc["boxscore"]["players"]:
        for group in team["statistics"]:
            if group.get("name") == "receiving":
                for a in group["athletes"]:
                    if a["athlete"]["id"] == MCBRIDE:
                        a["stats"][group["keys"].index("receptions")] = str(receptions)
    return doc


class SimClock:
    """One fake clock for the breakers, the rate limiter and the job's ticks."""

    def __init__(self, start: datetime, tick_seconds: float = 30.0):
        self.start, self.now, self.tick_seconds = start, start, tick_seconds

    def monotonic(self) -> float:
        return (self.now - self.start).total_seconds()

    def sleep(self, seconds: float) -> None:
        from datetime import timedelta
        self.now += timedelta(seconds=seconds)


class RecordingRouter:
    """Replays a game exported by `cli export-recording`: each call gets the next response that
    was recorded for that kind of request, and the last one again once they run out. The job
    makes the same calls at the same ticks as when it was recorded, so they line up."""

    def __init__(self, directory):
        from pathlib import Path

        self.breakers = None  # a replay has no providers to break
        root = Path(directory)
        self._frames = {"scoreboard": [], "summary": []}
        for entry in json.loads((root / "index.json").read_text()):
            self._frames[entry["kind"]].append(
                (entry["source"], json.loads((root / entry["file"]).read_text())))
        self._next = {"scoreboard": 0, "summary": 0}
        self.calls: list[tuple] = []

    def _take(self, kind):
        frames = self._frames[kind]
        source, payload = frames[min(self._next[kind], len(frames) - 1)]
        self._next[kind] += 1
        return DataSource(source), payload

    def scoreboard(self, sport, day, max_wait=0.0, *, only=None):
        self.calls.append(("board", day))
        provider, payload = self._take("scoreboard")
        return Routed(provider, espn.parse_scoreboard(sport, payload))

    def box_score(self, sport, espn_event_id, max_wait=0.0, *, only=None):
        self.calls.append(("box", espn_event_id))
        provider, payload = self._take("summary")
        return Routed(provider, espn.parse_box_score(sport, payload))

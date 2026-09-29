"""`poll_nfl_live`: live tracking of NFL games (SPEC.md sections 8.1 and 8.3).

Ticks every 30 seconds and makes only the requests that are due under the cadence table:
one scoreboard call per game day at the shortest interval any active game there needs, and a
box score per game with pending player legs. It keeps each event's status, scores and progress
key up to date through the integrity guards, and sets `live_value` / `live_source` on pending
legs for the Live page.

Live values never settle anything. Every failure leaves the last good data in place and the
next tick tries again: the schedule is the retry.
"""
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from parlaytracker.core import live, services
from parlaytracker.core.models import (
    DataSource,
    Event,
    EventStatus,
    FailureKind,
    Leg,
    LegResult,
    MarketType,
    Sport,
)
from parlaytracker.ingest import espn, guards
from parlaytracker.ingest.http import RateLimited
from parlaytracker.ingest.router import (
    AllProvidersFailed,
    EspnRouter,
    ProviderOpen,
    other_score_provider,
)
log = logging.getLogger("parlaytracker.worker")

# A pause between two requests in one tick keeps them a host-interval apart (section 6.1: one
# request every 2 seconds per host), so a busy Sunday isn't skipped by the rate limiter.
REQUEST_GAP = timedelta(seconds=2.1)
PLAYER_MARKETS = {m for m in MarketType if m.value.startswith("player_")}


@dataclass
class LiveSummary:
    scoreboards: int = 0
    boxes: int = 0
    probes: int = 0
    switched: int = 0   # a frozen feed replaced by the other provider's data
    skipped: int = 0    # due but not made: ESPN down, rate limited, or a breaker open


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _key(event: Event) -> guards.ProgressKey | None:
    return guards.progress_key(event.sport, event.status, event.period, event.clock_seconds)


class PollNflLive:
    def __init__(self, engine: Engine, router: EspnRouter,
                 sleep: Callable[[float], None] = time.sleep):
        self._engine = engine
        self._router = router
        self._sleep = sleep
        # In memory: a restart just makes everything due once, which is what we want.
        self._scoreboard_at: dict[date, datetime] = {}
        self._box_at: dict[int, datetime] = {}
        self._probe_at: dict[int, datetime] = {}
        self._provider: dict[date, DataSource] = {}  # who served each day's last scoreboard
        self._requests_this_tick = 0

    # --- the tick ---------------------------------------------------------------------------

    def __call__(self, now: datetime | None = None) -> LiveSummary:
        now = now or _utcnow()
        out = LiveSummary()
        self._requests_this_tick = 0
        with Session(self._engine, expire_on_commit=False) as session:
            events = [e for e in session.scalars(
                select(Event).where(
                    Event.sport == Sport.NFL,
                    Event.status.notin_([EventStatus.FINAL, EventStatus.POSTPONED,
                                         EventStatus.CANCELLED]),
                    select(Leg.id).where(Leg.event_id == Event.id,
                                         Leg.result == LegResult.PENDING).exists())
                .order_by(Event.start_time)) if live.is_active(e, now)]
            if not events:
                return out  # one cheap query when nothing is on
            by_day: dict[date, list[Event]] = defaultdict(list)
            for event in events:
                by_day[espn.game_day(event.start_time)].append(event)
            for day, group in by_day.items():
                self._scoreboard(session, day, group, now, out)
                session.commit()
            for event in events:
                self._box(session, event, now, out)
                session.commit()
        return out

    # --- requests ---------------------------------------------------------------------------

    def _pace(self) -> None:
        if self._requests_this_tick:
            self._sleep(REQUEST_GAP.total_seconds())
        self._requests_this_tick += 1

    def _due(self, last: datetime | None, interval: timedelta | None, now: datetime) -> bool:
        return interval is not None and (last is None or now - last >= interval)

    def _scoreboard(self, session: Session, day: date, group: list[Event], now: datetime,
                    out: LiveSummary) -> None:
        intervals = [live.scoreboard_interval(e.status) for e in group]
        interval = min((i for i in intervals if i is not None), default=None)
        if not self._due(self._scoreboard_at.get(day), interval, now):
            return
        self._pace()
        try:
            routed = self._router.scoreboard(Sport.NFL, day)
        except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
            log.info("live scoreboard %s skipped: %s", day, e)
            out.skipped += 1
            return
        out.scoreboards += 1
        self._scoreboard_at[day] = now
        self._provider[day] = routed.provider
        games = {g.espn_event_id: g for g in routed.value.games}
        for event in group:
            game = games.get(event.espn_event_id)
            if game is None:
                event.last_error = routed.value.errors.get(
                    event.espn_event_id, "not on ESPN's scoreboard for its game day")
                continue
            self._apply(session, event, game, routed.provider, now)
            if guards.is_frozen(event.status, event.last_progress_at, now):
                self._probe(session, day, event, now, out)

    def _apply(self, session: Session, event: Event, game: espn.Game, provider: DataSource,
               now: datetime) -> guards.Verdict | None:
        verdict = services.apply_event_reading(
            session, event, status=game.status, home_score=game.home_score,
            away_score=game.away_score, period=game.period, clock_seconds=game.clock_seconds,
            now=now)
        if verdict is guards.Verdict.STALE or verdict is None:
            return verdict  # older than what we hold: show nothing new from it
        for leg in event.legs:
            if leg.result is LegResult.PENDING and leg.market_type in live.TEAM_MARKETS:
                services.set_live_value(session, leg, live.score_live_value(
                    leg, event.home_score, event.away_score), provider, now)
        return verdict

    def _probe(self, session: Session, day: date, event: Event, now: datetime,
               out: LiveSummary) -> None:
        """A frozen feed (section 8.3): ask the other provider once. Only if it is *ahead* is
        the current provider frozen; if it shows the same, the game itself has stopped (a
        review, an injury) and nothing changes."""
        if not guards.may_probe(self._probe_at.get(event.id), now):
            return
        self._probe_at[event.id] = now
        current = self._provider.get(day, DataSource.ESPN_WEB)
        other = other_score_provider(current)
        self._pace()
        try:
            routed = self._router.scoreboard(Sport.NFL, day, only=other)
        except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
            log.info("frozen-feed probe of %s skipped: %s", other, e)
            return
        out.probes += 1
        game = next((g for g in routed.value.games if g.espn_event_id == event.espn_event_id),
                    None)
        if game is None:
            return
        probe_key = guards.progress_key(Sport.NFL, game.status, game.period, game.clock_seconds)
        ahead = probe_key is not None and (
            guards.compare(probe_key, _key(event)) is guards.Verdict.ADVANCE)
        if not ahead:
            log.info("event %s: no change for %s, and %s agrees: the game is stopped",
                     event.espn_event_id, now - (event.last_progress_at or now), other)
            return
        log.warning("event %s: %s is frozen and %s is ahead: switching", event.espn_event_id,
                    current, other)
        self._router.breakers.failure(current.value, FailureKind.FROZEN,
                                      f"feed frozen; {other.value} is ahead")
        self._provider[day] = routed.provider
        self._apply(session, event, game, routed.provider, now)
        out.switched += 1

    def _box(self, session: Session, event: Event, now: datetime, out: LiveSummary) -> None:
        legs = [leg for leg in event.legs
                if leg.result is LegResult.PENDING and leg.market_type in PLAYER_MARKETS]
        if not legs or event.status not in live.BOX_EVERY:
            return
        if not self._due(self._box_at.get(event.id), live.box_interval(event.status), now):
            return
        self._pace()
        try:
            routed = self._router.box_score(Sport.NFL, event.espn_event_id)
        except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
            log.info("live box score %s skipped: %s", event.espn_event_id, e)
            out.skipped += 1
            return
        out.boxes += 1
        self._box_at[event.id] = now
        for leg in legs:
            # A player missing from the table means neither zero nor void (section 6.1):
            # show "no stat line yet", never a made-up 0.
            value = routed.value.stats.get(leg.market_type, {}).get(leg.espn_athlete_id or "")
            services.set_live_value(session, leg, value, routed.provider, now)

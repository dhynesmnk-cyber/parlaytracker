"""Auto-settlement jobs (SPEC.md sections 7.1 and 8.1).

`check_finals` finds out which games have finished, `settle` settles their legs ten minutes
later, `recheck_settled` re-reads NBA, NHL and MLB box scores a day on, `verify_nfl` checks
NFL legs against nflverse, `canary` proves every provider still parses, and `prune_samples`
tidies the raw samples.

Every job takes `now` so tests can run it on a fake clock, returns a small summary, and
leaves a game it can't read for the next run. The worker never changes a settled result: a
disagreement goes to Review (section 7.1).
"""
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Engine, and_, delete, func, or_, select
from sqlalchemy.orm import Session, joinedload

from parlaytracker.core import services
from parlaytracker.core.models import (
    DataSource,
    Event,
    EventStatus,
    Leg,
    LegResult,
    MarketType,
    RawSample,
    Slip,
    Sport,
)
from parlaytracker.core.settlement import (
    NO_STAT_NO_SNAPS_REASON,
    PLAYED_NO_STAT_NOTE,
    MissingPlayer,
    resolve_missing_nfl_player,
    score_value,
)
from parlaytracker.ingest import espn
from parlaytracker.ingest.http import RateLimited
from parlaytracker.ingest.nflverse import NflverseData, NflverseError, nfl_season
from parlaytracker.ingest.router import (
    AllProvidersFailed,
    EspnRouter,
    ProviderOpen,
    SampleRecord,
)

log = logging.getLogger("parlaytracker.worker")

# A game is worth asking about from this long after kickoff (section 8.1).
EXPECTED_DURATION = {
    Sport.NFL: timedelta(hours=3), Sport.NBA: timedelta(hours=2, minutes=30),
    Sport.NHL: timedelta(hours=3), Sport.MLB: timedelta(hours=3),
}
STALE_AFTER = timedelta(hours=8)      # not final this long after the start: Review
RECHECK_AFTER = timedelta(hours=24)   # NBA, NHL, MLB stat corrections
RECHECK_WINDOW = timedelta(hours=1)
VERIFY_WITHIN = timedelta(days=7)
ESPN_UNAVAILABLE_AFTER = timedelta(hours=10)  # start + 4 h expected end + 6 h (section 7.1)
FAILURE_SAMPLES_KEPT = 5
RECORDING_KEPT = timedelta(days=30)

ESPN_SOURCES = (DataSource.ESPN_WEB, DataSource.ESPN_SITE, DataSource.ESPN_CDN)
CANARY_LOOKBACK_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _pending_legs():
    """Pending legs nobody is already looking at."""
    return and_(Leg.result == LegResult.PENDING, Leg.needs_review.is_(False))


@dataclass
class Summary:
    settled: int = 0
    flagged: int = 0
    verified: int = 0
    skipped: int = 0


def fmt(value: Decimal) -> str:
    """25.0 -> "25", 27.5 -> "27.5": the database keeps one decimal, people don't want to."""
    return format(value.normalize(), "f")


def score_leg_value(leg: Leg, home: int, away: int) -> Decimal:
    return Decimal(score_value(leg.market_type, leg.side, home, away))


def _value_from_box(leg: Leg, box: espn.BoxScore) -> Decimal | None:
    """The final value ESPN's box score gives this leg, or None if the player isn't in it."""
    if leg.market_type in (MarketType.GAME_TOTAL, MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD):
        assert box.home_score is not None and box.away_score is not None
        return score_leg_value(leg, box.home_score, box.away_score)
    return box.stats.get(leg.market_type, {}).get(leg.espn_athlete_id or "")


def _log_unavailable(what: str, e: Exception) -> None:
    if isinstance(e, ProviderOpen | RateLimited):
        log.info("%s skipped: %s", what, e)
    else:
        log.warning("%s failed: %s", what, e)


# --- check_finals ------------------------------------------------------------------------


class CheckFinals:
    """Every 15 minutes: which games with pending legs have finished?

    One scoreboard call per sport per game day. NFL is included until Phase 7's live poller
    takes it over during games; afterwards this is only a safety net for NFL.
    """

    def __init__(self, engine: Engine, router: EspnRouter):
        self._engine = engine
        self._router = router

    def __call__(self, now: datetime | None = None, *, force: bool = False,
                 backfill: bool = False) -> Summary:
        """`force` ignores the expected-duration wait; `backfill` dates a final that was
        found long after the fact to the game's expected end, so the ten-minute gate holds
        honestly."""
        now = now or _utcnow()
        out = Summary()
        with Session(self._engine, expire_on_commit=False) as session:
            events = session.scalars(
                select(Event).where(
                    Event.status.notin_([EventStatus.FINAL, EventStatus.POSTPONED,
                                         EventStatus.CANCELLED]),
                    Event.start_time <= now,
                    select(Leg.id).where(Leg.event_id == Event.id,
                                         Leg.result == LegResult.PENDING).exists())
            ).all()
            due = [e for e in events
                   if force or e.start_time + EXPECTED_DURATION[e.sport] <= now]
            groups: dict[tuple[Sport, object], list[Event]] = defaultdict(list)
            for event in due:
                groups[(event.sport, espn.game_day(event.start_time))].append(event)
            for (sport, day), group in groups.items():
                try:
                    result = self._router.scoreboard(sport, day).value
                except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
                    _log_unavailable(f"scoreboard {sport} {day}", e)
                    out.skipped += len(group)
                    continue
                games = {g.espn_event_id: g for g in result.games}
                for event in group:
                    game = games.get(event.espn_event_id)
                    if game is None:
                        event.last_error = result.errors.get(
                            event.espn_event_id, "not on ESPN's scoreboard for its game day")
                        out.skipped += 1
                        continue
                    final_at = None
                    if backfill and game.status is EventStatus.FINAL:
                        final_at = min(now, event.start_time + EXPECTED_DURATION[sport])
                    services.apply_event_reading(
                        session, event, status=game.status, home_score=game.home_score,
                        away_score=game.away_score, period=game.period,
                        clock_seconds=game.clock_seconds, now=now, final_at=final_at)
                session.commit()
        return out


# --- settle ------------------------------------------------------------------------------


class Settle:
    """Every 5 minutes: settle the legs of games that have been final for ten minutes, and
    flag games that will never settle by themselves."""

    def __init__(self, engine: Engine, router: EspnRouter):
        self._engine = engine
        self._router = router

    def __call__(self, now: datetime | None = None) -> Summary:
        now = now or _utcnow()
        out = Summary()
        with Session(self._engine, expire_on_commit=False) as session:
            self._flag_unsettleable(session, now, out)
            events = session.scalars(
                select(Event).where(
                    Event.status == EventStatus.FINAL,
                    Event.final_at <= now - services.FINAL_GATE,
                    select(Leg.id).where(Leg.event_id == Event.id, _pending_legs()).exists())
                .order_by(Event.start_time)
            ).all()
            for event in events:
                legs = list(session.scalars(
                    select(Leg).where(Leg.event_id == event.id, _pending_legs())
                    .options(joinedload(Leg.slip).selectinload(Slip.legs), joinedload(Leg.event))
                    .order_by(Leg.id)))
                self._settle_event(session, event, legs, now, out)
                session.commit()
            session.commit()
        return out

    def _flag_unsettleable(self, session: Session, now: datetime, out: Summary) -> None:
        """Postponed, cancelled and stale games, and `other` legs whose game is over."""
        for event in session.scalars(
                select(Event).where(
                    or_(Event.status.in_([EventStatus.POSTPONED, EventStatus.CANCELLED]),
                        and_(Event.status.notin_([EventStatus.FINAL, EventStatus.POSTPONED,
                                                  EventStatus.CANCELLED]),
                             Event.start_time <= now - STALE_AFTER)),
                    select(Leg.id).where(Leg.event_id == Event.id, _pending_legs()).exists())):
            reason = (f"Game {event.status.replace('_', ' ')}: settle or void by hand"
                      if event.status in (EventStatus.POSTPONED, EventStatus.CANCELLED)
                      else "Not final 8 hours after its start: check the result")
            for leg in session.scalars(select(Leg).where(Leg.event_id == event.id,
                                                         _pending_legs())):
                services.flag_leg(session, leg, reason)
                out.flagged += 1

    def _settle_event(self, session: Session, event: Event, legs: list[Leg], now: datetime,
                      out: Summary) -> None:
        for leg in [leg for leg in legs if leg.market_type is MarketType.OTHER]:
            services.flag_leg(session, leg, "Settle manually: this leg isn't tracked "
                                            "automatically")
            out.flagged += 1
        legs = [leg for leg in legs if leg.market_type is not MarketType.OTHER]
        if not legs:
            return
        try:
            routed = self._router.box_score(event.sport, event.espn_event_id)
        except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
            _log_unavailable(f"box score {event.espn_event_id}", e)
            out.skipped += len(legs)
            return
        box = routed.value
        if box.status is not EventStatus.FINAL or box.home_score is None \
                or box.away_score is None:
            log.info("event %s: box score says %s; not settling yet", event.espn_event_id,
                     box.status)
            out.skipped += len(legs)
            return
        services.apply_event_reading(session, event, status=box.status,
                                     home_score=box.home_score, away_score=box.away_score,
                                     now=now)
        for leg in legs:
            value = _value_from_box(leg, box)
            if value is not None:
                services.settle_leg_auto(session, leg, final_value=value,
                                         source=routed.provider, now=now)
                out.settled += 1
            elif event.sport is Sport.NFL:
                out.skipped += 1  # verify_nfl decides, from nflverse (section 7.1)
            else:
                reason = ("Did not play: enter 0 or void"
                          if leg.espn_athlete_id in box.did_not_play
                          else "No stat line: enter 0 or void")
                services.flag_leg(session, leg, reason)
                out.flagged += 1


# --- recheck_settled ---------------------------------------------------------------------


class RecheckSettled:
    """Every hour: NBA, NHL and MLB legs settled 24-25 hours ago get one more look at the
    box score. A changed value goes to Review; the settled result is never touched."""

    def __init__(self, engine: Engine, router: EspnRouter):
        self._engine = engine
        self._router = router

    def __call__(self, now: datetime | None = None) -> Summary:
        now = now or _utcnow()
        out = Summary()
        with Session(self._engine, expire_on_commit=False) as session:
            legs = session.scalars(
                select(Leg).join(Leg.event).where(
                    Event.sport != Sport.NFL,
                    Leg.settlement_source.in_(ESPN_SOURCES),
                    Leg.settled_at > now - RECHECK_AFTER - RECHECK_WINDOW,
                    Leg.settled_at <= now - RECHECK_AFTER,
                    Leg.final_value.is_not(None))
                .options(joinedload(Leg.event)).order_by(Leg.event_id, Leg.id)).all()
            by_event: dict[Event, list[Leg]] = defaultdict(list)
            for leg in legs:
                by_event[leg.event].append(leg)
            for event, event_legs in by_event.items():
                try:
                    box = self._router.box_score(event.sport, event.espn_event_id).value
                except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
                    _log_unavailable(f"recheck {event.espn_event_id}", e)
                    out.skipped += len(event_legs)
                    continue
                for leg in event_legs:
                    if box.home_score is None or box.away_score is None:
                        continue
                    now_value = _value_from_box(leg, box)
                    if now_value is not None and now_value != leg.final_value:
                        services.flag_leg(
                            session, leg,
                            f"Stat correction: was {fmt(leg.final_value)}, now {fmt(now_value)}")
                        out.flagged += 1
            session.commit()
        return out


# --- verify_nfl --------------------------------------------------------------------------


class VerifyNfl:
    """Daily at 10:00 ET, after nflverse's overnight publish (section 7.1).

    A: check ESPN-settled NFL legs from the last 7 days against nflverse.
    B: settle pending NFL legs ESPN's box score didn't cover, from nflverse.
    """

    def __init__(self, engine: Engine, nflverse: Callable[[], NflverseData]):
        self._engine = engine
        self._make_data = nflverse

    def __call__(self, now: datetime | None = None) -> Summary:
        now = now or _utcnow()
        out = Summary()
        data = self._make_data()
        with Session(self._engine, expire_on_commit=False) as session:
            try:
                self._verify_settled(session, data, now, out)
                self._settle_missing(session, data, now, out)
            except (NflverseError, ProviderOpen) as e:
                log.warning("verify_nfl stopped: %s", e)
            session.commit()
        return out

    def _nflverse_value(self, data: NflverseData, leg: Leg) -> Decimal | None:
        event = leg.event
        season = nfl_season(event.start_time)
        if leg.market_type in (MarketType.GAME_TOTAL, MarketType.TEAM_TOTAL,
                               MarketType.ALT_SPREAD):
            score = data.final_score(season, event.espn_event_id)
            return None if score is None else score_leg_value(leg, *score)
        return data.stat_value(season, event.espn_event_id, leg.espn_athlete_id or "",
                               leg.market_type)

    def _verify_settled(self, session: Session, data: NflverseData, now: datetime,
                        out: Summary) -> None:
        legs = session.scalars(
            select(Leg).join(Leg.event).where(
                Event.sport == Sport.NFL,
                Leg.result != LegResult.PENDING, Leg.verified_at.is_(None),
                Leg.needs_review.is_(False), Leg.final_value.is_not(None),
                Leg.settlement_source != DataSource.NFLVERSE,
                Leg.settled_at > now - VERIFY_WITHIN)
            .options(joinedload(Leg.event), joinedload(Leg.slip))).all()
        for leg in legs:
            theirs = self._nflverse_value(data, leg)
            if theirs is None:
                out.skipped += 1  # not published or not mapped: left unverified, not Review
            elif theirs == leg.final_value:
                services.verify_leg(session, leg, DataSource.NFLVERSE, now)
                out.verified += 1
            else:
                services.flag_leg(
                    session, leg,
                    f"Sources disagree: ESPN {fmt(leg.final_value)}, nflverse {fmt(theirs)}")
                out.flagged += 1

    def _settle_missing(self, session: Session, data: NflverseData, now: datetime,
                        out: Summary) -> None:
        """ESPN listed no stat line (or never finished): settle from nflverse (section 7.1)."""
        legs = session.scalars(
            select(Leg).join(Leg.event).where(
                Event.sport == Sport.NFL, _pending_legs(),
                Leg.market_type != MarketType.OTHER,
                or_(and_(Event.status == EventStatus.FINAL,
                         Event.final_at <= now - services.FINAL_GATE),
                    and_(Event.status != EventStatus.FINAL,
                         Event.status.notin_([EventStatus.POSTPONED, EventStatus.CANCELLED]),
                         Event.start_time <= now - ESPN_UNAVAILABLE_AFTER)))
            .options(joinedload(Leg.event), joinedload(Leg.slip).selectinload(Slip.legs))
            .order_by(Leg.event_id, Leg.id)).all()
        for leg in legs:
            event = leg.event
            season = nfl_season(event.start_time)
            if leg.market_type in (MarketType.GAME_TOTAL, MarketType.TEAM_TOTAL,
                                   MarketType.ALT_SPREAD):
                # ESPN says final and the leg is still pending only if settle hasn't run or
                # couldn't read the box score. Wait for it, unless ESPN never finished.
                if event.status is EventStatus.FINAL:
                    out.skipped += 1
                    continue
                value = self._nflverse_value(data, leg)
                if value is None:
                    out.skipped += 1
                    continue
                services.settle_leg_auto(session, leg, final_value=value,
                                         source=DataSource.NFLVERSE, now=now)
                out.settled += 1
                continue
            value = self._nflverse_value(data, leg)
            snaps = None if value is not None else data.offense_snaps(
                season, event.espn_event_id, leg.espn_athlete_id or "")
            action, settled_value = resolve_missing_nfl_player(
                value, snaps, event.start_time, now)
            match action:
                case MissingPlayer.SETTLE_FROM_NFLVERSE | MissingPlayer.SETTLE_ZERO_PLAYED:
                    note = PLAYED_NO_STAT_NOTE if action is MissingPlayer.SETTLE_ZERO_PLAYED \
                        else None
                    services.settle_leg_auto(session, leg, final_value=settled_value,
                                             source=DataSource.NFLVERSE, now=now, note=note)
                    out.settled += 1
                case MissingPlayer.REVIEW_NO_SNAPS:
                    services.flag_leg(session, leg, NO_STAT_NO_SNAPS_REASON)
                    out.flagged += 1
                case MissingPlayer.WAIT:
                    out.skipped += 1


# --- canary ------------------------------------------------------------------------------


class Canary:
    """At startup and daily at 09:00 ET: fully parse the latest completed NFL game from every
    ESPN provider, and load the nflverse datasets. A failure opens that provider's breaker
    with its failure kind, so the banner shows it before the next game day (section 8.1)."""

    def __init__(self, router: EspnRouter, nflverse: Callable[[], NflverseData]):
        self._router = router
        self._make_data = nflverse

    def __call__(self, now: datetime | None = None) -> dict[str, str]:
        """Returns provider -> "ok" or the reason it failed."""
        now = now or _utcnow()
        report: dict[str, str] = {}
        game = self._latest_completed_game(now)
        if game is None:
            log.info("canary: no completed NFL game in the last %d days", CANARY_LOOKBACK_DAYS)
        else:
            for provider in ESPN_SOURCES:
                try:
                    self._router.box_score(Sport.NFL, game.espn_event_id, max_wait=5.0,
                                           only=provider)
                    report[provider.value] = "ok"
                except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
                    report[provider.value] = str(e)
                    log.warning("canary: %s failed: %s", provider.value, e)
        try:
            self._make_data().warm(nfl_season(now))
            report["nflverse"] = "ok"
        except (NflverseError, ProviderOpen) as e:
            report["nflverse"] = str(e)
            log.warning("canary: nflverse failed: %s", e)
        return report

    def _latest_completed_game(self, now: datetime) -> espn.Game | None:
        for back in range(CANARY_LOOKBACK_DAYS):
            day = espn.game_day(now - timedelta(days=back))
            try:
                games = self._router.scoreboard(Sport.NFL, day, max_wait=5.0).value.games
            except (AllProvidersFailed, RateLimited, ProviderOpen) as e:
                _log_unavailable(f"canary scoreboard {day}", e)
                continue
            finals = [g for g in games if g.status is EventStatus.FINAL]
            if finals:
                return finals[-1]
        return None


# --- prune_samples -----------------------------------------------------------------------


def prune_samples(engine: Engine, now: datetime | None = None) -> int:
    """Keep the last 5 failure samples per provider; delete recordings older than 30 days."""
    now = now or _utcnow()
    deleted = 0
    with Session(engine) as session:
        ranked = select(
            RawSample.id,
            func.row_number().over(partition_by=RawSample.source,
                                   order_by=(RawSample.fetched_at.desc(),
                                             RawSample.id.desc())).label("n"),
        ).where(RawSample.reason == "failure").subquery()
        old_failures = select(ranked.c.id).where(ranked.c.n > FAILURE_SAMPLES_KEPT)
        deleted += session.execute(
            delete(RawSample).where(RawSample.id.in_(old_failures))).rowcount
        deleted += session.execute(delete(RawSample).where(
            RawSample.reason == "recording",
            RawSample.fetched_at < now - RECORDING_KEPT)).rowcount
        session.commit()
    return deleted


def sample_sink(engine: Engine) -> Callable[[SampleRecord], None]:
    """Where the router saves raw responses: the `raw_samples` table."""
    def save(record: SampleRecord) -> None:
        with Session(engine) as session:
            session.add(RawSample(
                source=record.source, reason=record.reason, url=record.url,
                status_code=record.status_code, espn_event_id=record.espn_event_id,
                error=record.error, body=record.body))
            session.commit()
    return save

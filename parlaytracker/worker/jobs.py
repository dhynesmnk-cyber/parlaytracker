"""Worker jobs (SPEC.md section 8.1). Phase 3: `heartbeat` and `capture_closing`.

Every job body is wrapped by `guarded`, so a job never raises into the scheduler.
"""
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload, selectinload

from parlaytracker.core import services
from parlaytracker.core.models import (
    ClosingSource,
    Event,
    FailureKind,
    HealthState,
    Leg,
    LegResult,
    MarketType,
    RawSample,
    Slip,
    SourceHealth,
)
from parlaytracker.ingest import closing
from parlaytracker.ingest.closing import Closing, LegQuery
from parlaytracker.ingest.http import FetchError, RateLimited
from parlaytracker.ingest.odds_api import EventOdds, OddsSource, RequestRejected
from parlaytracker.ingest.resolve import MARKET_KEYS, same_game
from parlaytracker.ingest.router import Breakers, ProviderOpen

log = logging.getLogger("parlaytracker.worker")

# Closing lines are captured this long before kickoff (section 8.2, step 1).
CAPTURE_WINDOW = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def heartbeat(engine: Engine, now: datetime | None = None,
              breakers: Breakers | None = None) -> None:
    """Show the worker is alive, and write every provider's breaker state and counters."""
    now = now or _utcnow()
    stmt = insert(SourceHealth).values(source="worker", state=HealthState.OK, last_success_at=now,
                                       consecutive_failures=0, requests_last_hour=0,
                                       errors_last_hour=0)
    stmt = stmt.on_conflict_do_update(index_elements=[SourceHealth.source],
                                      set_={"last_success_at": now, "state": HealthState.OK})
    with engine.begin() as conn:
        conn.execute(stmt)
    if breakers is not None:
        breakers.persist()


def guarded(engine: Engine, name: str, job: Callable[[], object]) -> Callable[[], None]:
    """Wrap a job: log and record any error, never raise."""
    def run() -> None:
        try:
            job()
        except Exception as e:
            log.exception("job %s failed", name)
            try:
                with engine.begin() as conn:
                    conn.execute(insert(SourceHealth).values(
                        source="worker", state=HealthState.OK, last_error=f"{name}: {e}"[:500],
                        consecutive_failures=0, requests_last_hour=0, errors_last_hour=0,
                    ).on_conflict_do_update(index_elements=[SourceHealth.source],
                                            set_={"last_error": f"{name}: {e}"[:500]}))
            except Exception:
                log.exception("could not record the error from job %s", name)
    return run


# --- capture_closing (section 8.2) -----------------------------------------------------------


def _affordable(quota_remaining: int | None, cost: int, reserve: int) -> bool:
    """Budget check. An unknown quota (before the first response) is allowed through."""
    return quota_remaining is None or quota_remaining - cost >= reserve


def _query(leg: Leg) -> LegQuery:
    assert leg.line is not None  # only OTHER legs lack a line, and they are never selected
    return LegQuery(leg.market_type, leg.line, leg.side, leg.player_name,
                    leg.slip.sportsbook.odds_api_key)


class ClosingCapture:
    """The `capture_closing` job. It keeps one bit of state: events already tried.

    A game gets one capture attempt per successful response, so a leg the API has no line for
    isn't retried (and paid for) every minute until kickoff. A restart forgets it, which costs
    at most one repeat.
    """

    def __init__(self, engine: Engine, odds: OddsSource, breakers: Breakers, reserve: int):
        self._engine = engine
        self._odds = odds
        self._breakers = breakers
        self._reserve = reserve
        self._attempted: dict[int, datetime] = {}  # event id -> start time

    def __call__(self, now: datetime | None = None) -> int:
        """Capture what is due. Returns the number of legs that got a closing line."""
        now = now or _utcnow()
        self._attempted = {k: v for k, v in self._attempted.items() if v > now}
        with Session(self._engine, expire_on_commit=False) as session:
            legs = session.scalars(
                select(Leg).join(Leg.event)
                .where(Leg.result == LegResult.PENDING, Leg.market_type != MarketType.OTHER,
                       Leg.closing_captured_at.is_(None),
                       Event.start_time > now, Event.start_time <= now + CAPTURE_WINDOW)
                .options(joinedload(Leg.event),
                         selectinload(Leg.slip).joinedload(Slip.sportsbook))
                .order_by(Event.start_time, Leg.id)
            ).all()
            if not legs:
                return 0
            by_event: dict[Event, list[Leg]] = defaultdict(list)
            for leg in legs:
                if leg.event_id not in self._attempted:
                    by_event[leg.event].append(leg)
            captured = 0
            events_by_sport: dict = {}
            for event, event_legs in by_event.items():
                try:
                    captured += self._capture_event(session, event, event_legs, events_by_sport,
                                                    now)
                except ProviderOpen as e:
                    log.info("closing lines skipped: %s", e)
                    break
                except RateLimited as e:
                    log.warning("closing lines delayed: %s", e)
                    break
                except (FetchError, RequestRejected) as e:
                    self._save_failure_sample(session, e)
                    log.warning("closing lines for event %s failed: %s", event.espn_event_id, e)
                finally:
                    session.commit()
        self._breakers.persist("odds_api")  # the quota, without waiting for the heartbeat
        return captured

    def _capture_event(self, session: Session, event: Event, legs: list[Leg],
                       events_by_sport: dict, now: datetime) -> int:
        if event.odds_api_event_id is None and not self._resolve_event(event, events_by_sport):
            self._attempted[event.id] = event.start_time
            log.warning("no Odds API game for event %s (%s @ %s): flagged for manual entry",
                        event.espn_event_id, event.away_team, event.home_team)
            return 0
        queries = [(leg, _query(leg)) for leg in legs]

        main_keys = sorted({MARKET_KEYS[q.market_type].main for _, q in queries})
        if not _affordable(self._odds.quota_remaining, len(main_keys), self._reserve):
            self._attempted[event.id] = event.start_time
            log.warning("closing lines for event %s skipped: %s credits left, reserve %s",
                        event.espn_event_id, self._odds.quota_remaining, self._reserve)
            return 0
        first = self._odds.event_odds(event.sport, event.odds_api_event_id, main_keys)
        self._attempted[event.id] = event.start_time

        found: dict[int, Closing] = {}
        for leg, q in queries:
            if hit := closing.find_exact_main(q, first):
                found[leg.id] = hit
        self._follow_up(event, queries, first, found)
        for leg, q in queries:
            if leg.id not in found:
                hit = closing.find_book_main(q, first) or closing.find_median_main(q, first)
                if hit:
                    found[leg.id] = hit

        captured = 0
        for leg, _ in queries:
            hit = found.get(leg.id)
            if hit is None:
                log.warning("no closing line found for leg %s: flagged for manual entry", leg.id)
                continue
            try:
                services.set_closing_line(
                    session, leg, closing_line=hit.line, closing_odds=hit.odds,
                    closing_opposite_odds=hit.opposite_odds, source=ClosingSource.ODDS_API,
                    now=now)
            except services.ServiceError as e:
                log.warning("closing line for leg %s rejected: %s", leg.id, e)
                continue
            captured += 1
        return captured

    def _follow_up(self, event: Event, queries: list[tuple[Leg, LegQuery]], first: EventOdds,
                   found: dict[int, Closing]) -> None:
        """One call per event for the alternate markets the legs still need (step 5.2)."""
        need = [(leg, q) for leg, q in queries
                if leg.id not in found and closing.needs_alternate_call(q, first)]
        if not need:
            return
        keys = sorted({MARKET_KEYS[q.market_type].alternate for _, q in need})
        if not _affordable(self._odds.quota_remaining, len(keys), self._reserve):
            log.warning("alternate lines for event %s skipped: %s credits left, reserve %s",
                        event.espn_event_id, self._odds.quota_remaining, self._reserve)
            return
        try:
            second = self._odds.event_odds(event.sport, event.odds_api_event_id, keys)
        except (FetchError, RequestRejected, ProviderOpen, RateLimited) as e:
            log.warning("alternate lines for event %s failed: %s", event.espn_event_id, e)
            return  # the fallbacks below still use the first response
        for leg, q in need:
            if hit := closing.find_exact_alternate(q, second):
                found[leg.id] = hit

    def _resolve_event(self, event: Event, events_by_sport: dict) -> bool:
        """Find and cache `odds_api_event_id` (section 6.2). The list call costs 0 credits."""
        if event.sport not in events_by_sport:
            events_by_sport[event.sport] = self._odds.events(event.sport)
        for api in events_by_sport[event.sport]:
            if same_game(event.home_team, event.away_team, event.start_time,
                         api.home_team, api.away_team, api.commence_time):
                event.odds_api_event_id = api.id
                return True
        return False

    def _save_failure_sample(self, session: Session, error: Exception) -> None:
        """A response that didn't fit the model is kept for the parser fix (section 8.3)."""
        if isinstance(error, FetchError) and error.body and error.kind in (
                FailureKind.SCHEMA, FailureKind.IMPLAUSIBLE):
            session.add(RawSample(source="odds_api", reason="failure", url=error.url,
                                  status_code=error.status_code, error=error.detail,
                                  body=error.body[:1_000_000]))

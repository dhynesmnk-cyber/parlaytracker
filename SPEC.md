# ParlayTracker — Development Specification (v2)

This replaces the previous specification and all code currently in this repository. It is written for a coding agent: build the phases in section 13 in order, and do not start a phase until the previous one meets its exit criteria in CI.

---

## 1. Product

ParlayTracker is a private, two-user analytics tool for US sports betting. It records two kinds of slip:

- **Placed**: a wager actually made. It can be a single, a parlay or a same-game parlay (SGP).
- **Unplaced**: an opportunity the users spotted but did not bet.

Every tracked selection is an **Over**. Over time, the pooled history shows where the users have a genuine edge.

Priorities, in order:
1. The data is correct.
2. Failures are visible, never silent.
3. Logging is fast.
4. Running costs stay near zero.

### 1.1 Fixed decisions

| Decision | Choice |
|---|---|
| Bet types | Singles, parlays and SGPs |
| Users | Two, identified by their Tailscale login. Analytics are pooled; every slip records who logged it |
| Hosting | A dedicated always-on laptop at home running Docker Compose (web, worker, Postgres), reachable only over Tailscale (section 12) |
| AI | Qwen models only, through OpenRouter's OpenAI-compatible API (section 6.3). No Anthropic models |
| Frontend | Streamlit only |
| Live tracking | NFL only. NBA, NHL and MLB slips are logged and settled after the game, but not tracked during it |
| Data ingestion | Polling only |
| Sports and odds data | Free sources only, and no scraping |

### 1.2 Scope

Sports: NFL, NBA, MLB, NHL.

| Market (`MarketType`) | Sports | Settles on |
|---|---|---|
| `game_total` | all | Home + away final score, including OT and extra innings |
| `team_total` | all | The chosen side's final score |
| `alt_spread` | all | The chosen side's winning margin (section 7.1) |
| `player_receptions` | NFL | Receptions |
| `player_receiving_yards` | NFL | Receiving yards |
| `player_rushing_yards` | NFL | Rushing yards |
| `player_passing_yards` | NFL | Passing yards |
| `player_points` | NBA, NHL | NBA: points. NHL: goals + assists |
| `other` | any | Manual settlement only |

`other` exists only so that a parlay containing an out-of-scope leg (an Under, a moneyline and so on) can be stored and settled correctly. `other` legs are settled by hand and are excluded from all selection analytics.

### 1.3 Out of scope

- Bet recommendations or suggestions
- Bankroll management
- Notifications
- Scraping any website
- Historical odds backfill
- Unders as tracked selections
- Sports other than the four above
- A separate API server or a JavaScript frontend

---

## 2. Architecture

```
phone ──▶ Tailscale Serve ──▶ web (Streamlit) ──▶ Qwen via OpenRouter  (screenshot upload only)
                                   │           ──▶ ESPN                 (game and roster pickers, cached)
                                   ▼
                              PostgreSQL ◀────── worker (APScheduler) ──▶ ESPN, with host failover  (NFL live; finals for all sports)
                                                                      ──▶ nflverse                  (next-day NFL verification)
                                                                      ──▶ The Odds API              (closing lines)
```

Everything in the box from Tailscale Serve to PostgreSQL runs on one laptop (section 12).

- There are two processes and one database. The processes share nothing except Postgres.
- **web** is the UI. It listens only on the laptop itself; Tailscale Serve is the only way in (section 9.1).
- **worker** runs every background job. It is always on and holds no state except in-memory backoff timers, so after a restart it rebuilds everything from the database.
- There is no FastAPI and no other service.

### 2.1 Stack

- Python 3.12
- Streamlit ≥ 1.42. The features used are `st.navigation`, `st.fragment(run_every=...)` and `st.context.headers`
- SQLAlchemy 2.0 (typed ORM), Alembic, psycopg 3
- PostgreSQL 16, in a Docker container on the laptop. The code only knows `DATABASE_URL`
- APScheduler 3.x, pinned `<4` because 4.x has a different API
- httpx for all outbound HTTP
- The `openai` SDK, pointed at OpenRouter's OpenAI-compatible endpoint for Qwen
- rapidfuzz, pandas, plotly, Pillow, pydantic ≥ 2, pydantic-settings
- `nflreadpy`, used only for next-day NFL verification (section 6.4)
- Docker Compose and Tailscale on the laptop (section 12)
- Dev: pytest, respx, ruff
- **Not used:** FastAPI, streamlit-autorefresh, nfl_data_py (deprecated), nba_api, Playwright

### 2.2 Layout

```
parlaytracker/
  core/
    config.py        # pydantic-settings; the only place environment variables are read
    db.py            # engine and session factory
    models.py        # section 4
    schemas.py       # section 5
    odds.py          # pure odds maths (section 7.3)
    settlement.py    # pure settlement functions (sections 7.1 and 7.2)
    analytics.py     # aggregation functions (section 10)
    services.py      # the ONLY functions that write slips and legs
  ingest/
    http.py          # shared httpx client, headers, timeouts, rate limiter (section 8.3)
    router.py        # per-provider circuit breakers and ESPN host failover (section 8.3)
    guards.py        # progress key, plausibility bounds, freeze detection (section 8.3)
    espn.py          # client + parsers returning typed dataclasses
    nflverse.py      # nflreadpy loaders and ESPN ID mapping (section 6.4)
    odds_api.py      # client + parsers
    extraction.py    # Qwen call + ExtractedSlip parsing
    resolve.py       # alias tables (markets, teams, sportsbooks) and player fuzzy matching
  worker/
    __main__.py      # BlockingScheduler, job registration, single-instance lock
    jobs.py
  app/
    main.py          # st.navigation, auth guard, health banner
    auth.py
    components/slip_form.py   # the ONE slip form, used by both Log and Screenshot
    pages/log.py  screenshot.py  live.py  analytics.py  review.py  settings.py
  cli.py             # maintenance commands, e.g. export a raw sample as a test fixture
migrations/          # Alembic
deploy/              # laptop setup, Docker Compose, auto-update and backup (section 12)
tests/
  fixtures/espn/  fixtures/odds_api/  fixtures/qwen/
  unit/  db/  worker/  app/
```

Rules:
- Pages call `services` and `analytics`. They never build SQL or write to models directly.
- `odds.py` and `settlement.py` are pure and need no database to test.

---

## 3. Configuration

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | `config.py` rewrites `postgres://` and `postgresql://` to `postgresql+psycopg://` |
| `DISPLAY_TZ` | yes | An IANA name such as `Europe/London`. Used only for display |
| `ODDS_API_KEY` | from phase 3 | |
| `ODDS_API_RESERVE` | no | Default 50. Credits below this are never spent automatically |
| `QWEN_API_KEY` | from phase 6 | An OpenRouter API key |
| `QWEN_BASE_URL` | no | Default `https://openrouter.ai/api/v1` |
| `QWEN_VISION_MODEL` | no | Default `qwen/qwen3-vl-32b-instruct` (section 6.3) |
| `MIN_SAMPLE` | no | Default 30 (section 10.3) |
| `RECORD_EVENT_IDS` | no | Comma-separated ESPN event IDs. Every response for these events is saved to `raw_samples` (section 8.3) |
| `ALLOWED_LOGINS` | yes, for web | Comma-separated Tailscale login names allowed to use the app, compared case-insensitively (section 9.1). If it is empty, nobody gets in |
| `DEV_LOGIN` | no | Local development only: the login to assume when no Tailscale header is present. Never set on the laptop |

- Every timestamp is stored in UTC (`timestamptz`) and converted to `DISPLAY_TZ` only in the UI.
- A "game day" for ESPN scoreboard requests is the US Eastern calendar date, so a Sunday-night NFL game belongs to Sunday. Verified: `dates=20260927` includes the 8:20pm ET game that starts at 00:20 UTC on the Monday.

---

## 4. Database schema

Design notes:
- A **slip** is what was, or would have been, submitted to a sportsbook. A **leg** is one selection on it. A single is a slip with one leg.
- Games are resolved to ESPN event IDs **at entry time**: the user picks the game from a list. Players are resolved to ESPN athlete IDs the same way. The worker never matches names.
- Team markets store `side` (home or away) rather than a team name, so no name matching is ever needed after entry.
- Enums are stored as VARCHAR plus a CHECK constraint, not as native Postgres enums, so adding a value is a simple migration.
- Money is `Numeric(12,2)`, lines are `Numeric(6,1)` and odds are integer American odds.
- The database enforces the invariants it can through the CHECK constraints below. The Pydantic models in section 5 enforce the rest before any write.
- Screenshot images are not stored.
- Every live, settled and verified value records which provider it came from (`live_source`, `settlement_source`, `verified_source`).
- Tags attach to legs and are for subjective context only. Section 10.2 lists the dimensions that already exist as columns; these must never be tagged.

The code below has been run against PostgreSQL 16 (and SQLite): every valid case is accepted, and every constraint and validation case listed in section 11 is rejected.

```python
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Enum, ForeignKey, Index, MetaData, Numeric,
    String, Table, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Sport(enum.StrEnum):
    NFL = "nfl"
    NBA = "nba"
    MLB = "mlb"
    NHL = "nhl"


class MarketType(enum.StrEnum):
    GAME_TOTAL = "game_total"
    TEAM_TOTAL = "team_total"
    ALT_SPREAD = "alt_spread"
    PLAYER_RECEPTIONS = "player_receptions"
    PLAYER_RECEIVING_YARDS = "player_receiving_yards"
    PLAYER_RUSHING_YARDS = "player_rushing_yards"
    PLAYER_PASSING_YARDS = "player_passing_yards"
    PLAYER_POINTS = "player_points"
    OTHER = "other"  # out-of-scope parlay leg (Under, moneyline): manual settle, not analysed


class TeamSide(enum.StrEnum):
    HOME = "home"
    AWAY = "away"


class SlipType(enum.StrEnum):
    SINGLE = "single"
    PARLAY = "parlay"
    SGP = "sgp"


class LegResult(enum.StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class SlipStatus(enum.StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    CASHED_OUT = "cashed_out"


class EventStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    BREAK = "break"      # halftime, end of period
    DELAYED = "delayed"  # weather or other stoppage
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class DataSource(enum.StrEnum):
    ESPN_WEB = "espn_web"    # site.web.api.espn.com
    ESPN_SITE = "espn_site"  # site.api.espn.com
    ESPN_CDN = "espn_cdn"    # cdn.espn.com (only if verified, section 15)
    NFLVERSE = "nflverse"
    MANUAL = "manual"


class HealthState(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # recent failures, breaker still closed
    OPEN = "open"          # breaker open until open_until


class FailureKind(enum.StrEnum):
    TRANSIENT = "transient"
    BLOCKED = "blocked"
    THROTTLED = "throttled"
    SCHEMA = "schema"
    IMPLAUSIBLE = "implausible"
    FROZEN = "frozen"


class EntrySource(enum.StrEnum):
    QUICK_ADD = "quick_add"
    SCREENSHOT = "screenshot"


class ClosingSource(enum.StrEnum):
    ODDS_API = "odds_api"
    MANUAL = "manual"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    # VARCHAR + CHECK instead of a native Postgres ENUM: adding a value later is a
    # one-line constraint migration instead of ALTER TYPE.
    return Enum(e, name=name, native_enum=False, create_constraint=True, length=32,
                values_callable=lambda members: [m.value for m in members])


TZ = DateTime(timezone=True)


class Sportsbook(Base):
    __tablename__ = "sportsbooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    odds_api_key: Mapped[str | None] = mapped_column(String(50))  # e.g. "draftkings"


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(_enum(Sport, "sport"))
    espn_event_id: Mapped[str] = mapped_column(String(20), unique=True)
    odds_api_event_id: Mapped[str | None] = mapped_column(String(64))
    home_team: Mapped[str] = mapped_column(String(60))  # ESPN display name
    away_team: Mapped[str] = mapped_column(String(60))
    home_espn_team_id: Mapped[str] = mapped_column(String(10))
    away_espn_team_id: Mapped[str] = mapped_column(String(10))
    start_time: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[EventStatus] = mapped_column(
        _enum(EventStatus, "event_status"), default=EventStatus.SCHEDULED)
    home_score: Mapped[int | None]
    away_score: Mapped[int | None]
    period: Mapped[int | None]         # progress key (section 8.3); OT is period >= 5
    clock_seconds: Mapped[int | None]  # game clock remaining in the period
    final_at: Mapped[datetime | None] = mapped_column(TZ)
    last_polled_at: Mapped[datetime | None] = mapped_column(TZ)
    last_progress_at: Mapped[datetime | None] = mapped_column(TZ)  # progress key last advanced
    last_error: Mapped[str | None] = mapped_column(Text)

    legs: Mapped[list["Leg"]] = relationship(back_populates="event")


class Slip(Base):
    __tablename__ = "slips"
    id: Mapped[int] = mapped_column(primary_key=True)
    logged_by: Mapped[str] = mapped_column(String(255))  # st.user.email
    is_placed: Mapped[bool]
    slip_type: Mapped[SlipType] = mapped_column(_enum(SlipType, "slip_type"))
    sportsbook_id: Mapped[int] = mapped_column(ForeignKey("sportsbooks.id"))
    american_odds: Mapped[int]  # total odds exactly as shown on the slip
    boosted: Mapped[bool] = mapped_column(default=False)
    stake: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    potential_payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[SlipStatus] = mapped_column(
        _enum(SlipStatus, "slip_status"), default=SlipStatus.PENDING)
    payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # total returned, incl. stake
    needs_review: Mapped[bool] = mapped_column(default=False)
    review_reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[EntrySource] = mapped_column(_enum(EntrySource, "entry_source"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(TZ)

    sportsbook: Mapped[Sportsbook] = relationship()
    legs: Mapped[list["Leg"]] = relationship(
        back_populates="slip", cascade="all, delete-orphan", order_by="Leg.id")

    __table_args__ = (
        CheckConstraint("american_odds >= 100 OR american_odds <= -100", name="odds_valid"),
        CheckConstraint("NOT is_placed OR stake IS NOT NULL", name="placed_has_stake"),
    )


leg_tags = Table(
    "leg_tags", Base.metadata,
    Column("leg_id", ForeignKey("legs.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))

    legs: Mapped[list["Leg"]] = relationship(secondary=leg_tags, back_populates="tags")

    __table_args__ = (UniqueConstraint("category", "name", name="uq_tags_category_name"),)


class Leg(Base):
    __tablename__ = "legs"
    id: Mapped[int] = mapped_column(primary_key=True)
    slip_id: Mapped[int] = mapped_column(ForeignKey("slips.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    market_type: Mapped[MarketType] = mapped_column(_enum(MarketType, "market_type"))
    side: Mapped[TeamSide | None] = mapped_column(_enum(TeamSide, "team_side"))
    espn_athlete_id: Mapped[str | None] = mapped_column(String(20))
    player_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)  # required for OTHER legs
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))  # NULL only for OTHER legs
    american_odds: Mapped[int | None]  # NULL only allowed on SGP legs
    result: Mapped[LegResult] = mapped_column(
        _enum(LegResult, "leg_result"), default=LegResult.PENDING)
    live_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    live_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    live_source: Mapped[DataSource | None] = mapped_column(_enum(DataSource, "live_source"))
    final_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    settled_at: Mapped[datetime | None] = mapped_column(TZ)
    settlement_source: Mapped[DataSource | None] = mapped_column(
        _enum(DataSource, "settlement_source"))
    verified_at: Mapped[datetime | None] = mapped_column(TZ)  # a second source agreed
    verified_source: Mapped[DataSource | None] = mapped_column(
        _enum(DataSource, "verified_source"))
    closing_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    closing_odds: Mapped[int | None]
    closing_opposite_odds: Mapped[int | None]  # the Under / other side, for no-vig CLV
    closing_source: Mapped[ClosingSource | None] = mapped_column(
        _enum(ClosingSource, "closing_source"))
    closing_captured_at: Mapped[datetime | None] = mapped_column(TZ)
    needs_review: Mapped[bool] = mapped_column(default=False)
    review_reason: Mapped[str | None] = mapped_column(Text)

    slip: Mapped[Slip] = relationship(back_populates="legs")
    event: Mapped[Event] = relationship(back_populates="legs")
    tags: Mapped[list[Tag]] = relationship(secondary=leg_tags, back_populates="legs")

    __table_args__ = (
        CheckConstraint(
            "american_odds IS NULL OR american_odds >= 100 OR american_odds <= -100",
            name="odds_valid"),
        CheckConstraint(
            "(market_type IN ('team_total', 'alt_spread')) = (side IS NOT NULL)",
            name="side_iff_team_market"),
        CheckConstraint(
            "(market_type LIKE 'player_%') = (espn_athlete_id IS NOT NULL)",
            name="athlete_iff_player_market"),
        CheckConstraint(
            "market_type = 'other' OR line IS NOT NULL", name="line_required"),
        CheckConstraint(
            "market_type <> 'other' OR description IS NOT NULL", name="other_has_description"),
        CheckConstraint(
            "(result = 'pending') = (settlement_source IS NULL)", name="settled_has_source"),
        CheckConstraint(
            "(verified_at IS NULL) = (verified_source IS NULL)", name="verified_pair"),
    )


class SourceHealth(Base):
    __tablename__ = "source_health"
    # One row per provider: "espn_web", "espn_site", "espn_cdn", "nflverse", "odds_api",
    # plus "worker" for the heartbeat. Written by the worker, read by the UI banner.
    source: Mapped[str] = mapped_column(String(30), primary_key=True)
    state: Mapped[HealthState] = mapped_column(
        _enum(HealthState, "health_state"), default=HealthState.OK)
    failure_kind: Mapped[FailureKind | None] = mapped_column(_enum(FailureKind, "failure_kind"))
    open_until: Mapped[datetime | None] = mapped_column(TZ)
    last_success_at: Mapped[datetime | None] = mapped_column(TZ)
    last_failure_at: Mapped[datetime | None] = mapped_column(TZ)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    requests_last_hour: Mapped[int] = mapped_column(default=0)
    errors_last_hour: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    quota_remaining: Mapped[int | None]  # Odds API x-requests-remaining


class RawSample(Base):
    """Raw response bodies: parse failures (last 5 per source) and recordings (section 8.3)."""
    __tablename__ = "raw_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(20))  # "failure" or "recording"
    url: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None]
    espn_event_id: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)  # truncated to 1 MB before insert
    fetched_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint("reason IN ('failure', 'recording')", name="reason_valid"),
        Index("ix_raw_samples_source_fetched_at", "source", "fetched_at"),
    )
```

Alembic:
- Set `target_metadata = Base.metadata`. The naming convention gives autogenerate stable constraint names.
- Migration `0001` creates everything above and seeds `sportsbooks`: DraftKings (`draftkings`), FanDuel (`fanduel`), BetMGM (`betmgm`) and Caesars (`williamhill_us`). Verify the Odds API keys (section 15).

---

## 5. Validation gate

There are two Pydantic layers, and they have different jobs:

- **`ExtractedSlip`** is loose. Every field is optional, and it describes whatever the vision model returned. It is never written to the database.
- **`SlipIn` / `LegIn`** are strict. Every slip, whether typed into the Log form or pre-filled from a screenshot, is validated as `SlipIn` inside `services.create_slip()`. That is the only function that inserts slips, and there is no other write path.

```python
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from parlaytracker.core.models import EntrySource, MarketType, SlipType, TeamSide

PLAYER_MARKETS = {m for m in MarketType if m.value.startswith("player_")}
TEAM_MARKETS = {MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD}


def _american(v: int | None) -> int | None:
    if v is not None and -100 < v < 100:
        raise ValueError("American odds must be <= -100 or >= +100")
    return v


class LegIn(BaseModel):
    event_id: int
    market_type: MarketType
    side: TeamSide | None = None
    espn_athlete_id: str | None = None
    player_name: str | None = None
    description: str | None = None
    line: Decimal | None = None
    american_odds: int | None = None
    tag_ids: list[int] = []

    _odds = field_validator("american_odds")(_american)

    @field_validator("line")
    @classmethod
    def _half_step(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and (v * 2) % 1 != 0:
            raise ValueError("line must be a whole or half number")
        return v

    @model_validator(mode="after")
    def _market_fields(self) -> "LegIn":
        is_player = self.market_type in PLAYER_MARKETS
        is_team = self.market_type in TEAM_MARKETS
        if is_player != (self.espn_athlete_id is not None):
            raise ValueError("player markets need a player; other markets must not have one")
        if is_team != (self.side is not None):
            raise ValueError("team total / alt spread need a side; other markets must not have one")
        if self.market_type is MarketType.OTHER:
            if not self.description:
                raise ValueError("an 'other' leg needs a description")
            return self
        if self.line is None:
            raise ValueError("line is required")
        if self.market_type is not MarketType.ALT_SPREAD and self.line <= 0:
            raise ValueError("Over lines must be positive")
        return self

    def selection_key(self) -> tuple:
        return (self.event_id, self.market_type, self.side, self.espn_athlete_id, self.line,
                self.description)


class SlipIn(BaseModel):
    is_placed: bool
    slip_type: SlipType
    sportsbook_id: int
    american_odds: int
    boosted: bool = False
    stake: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    potential_payout: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    source: EntrySource
    notes: str | None = None
    legs: list[LegIn] = Field(min_length=1)

    _odds = field_validator("american_odds")(_american)

    @model_validator(mode="after")
    def _slip_rules(self) -> "SlipIn":
        if self.is_placed and self.stake is None:
            raise ValueError("a placed bet needs a stake")
        if self.slip_type is SlipType.SINGLE:
            if len(self.legs) != 1:
                raise ValueError("a single has exactly one leg")
            if self.legs[0].american_odds != self.american_odds:
                raise ValueError("a single's leg odds must equal the slip odds")
        elif len(self.legs) < 2:
            raise ValueError("a parlay needs at least two legs")
        missing_odds = any(leg.american_odds is None for leg in self.legs)
        if self.slip_type is SlipType.PARLAY and missing_odds:
            raise ValueError("every leg of a standard parlay needs its own odds")
        if self.slip_type is SlipType.SGP and len({leg.event_id for leg in self.legs}) != 1:
            raise ValueError("every leg of a same-game parlay must be in the same game")
        keys = [leg.selection_key() for leg in self.legs]
        if len(keys) != len(set(keys)):
            raise ValueError("the same selection appears twice on this slip")
        return self


# Loose model for the vision model's output: everything optional, never the DB gate.
class ExtractedLeg(BaseModel):
    sport: str | None = None
    event_text: str | None = None
    team_text: str | None = None
    player_name: str | None = None
    market_text: str | None = None
    line: float | None = None
    american_odds: int | None = None


class ExtractedSlip(BaseModel):
    sportsbook_text: str | None = None
    slip_type_text: str | None = None
    american_odds: int | None = None
    stake: float | None = None
    potential_payout: float | None = None
    legs: list[ExtractedLeg] = []
```

The form also shows these **warnings**. They do not block saving:
- **Standard parlay, not boosted:** if the product of the legs' decimal odds differs from the slip's decimal odds by more than 2%, show "Slip odds don't match the legs. Boosted, or a typo?"
- **`potential_payout` entered:** if it differs from `stake × decimal(slip odds)` by more than $0.05, show "Payout doesn't match stake × odds."
- **Duplicate:** if the same selection (same `selection_key()`) is already in the database, show "Already logged by {user} at {odds}."

---

## 6. External data sources

### 6.1 ESPN: schedules, scores, box scores, rosters

This API is unofficial, free and needs no key. One parser covers all four sports. ESPN has been tightening access:
- In August 2026, `site.api.espn.com` began answering some clients with 403 "You don't have permission". Switching to `site.web.api.espn.com`, or replacing a browser User-Agent with a non-browser one, fixed it.
- Frequent polling also produced 403s.
- In September 2026, date-range queries (`dates=A-B`) stopped working. Single dates still work.
- **Observed on 2026-09-28 from a cloud server:** `site.api.espn.com` returned Akamai's HTML "Access Denied" (403), while `site.web.api.espn.com` and `cdn.espn.com` returned 200. The laptop's home connection may fare better; the canary shows it on first start.

So the same paths are served from several hosts, each treated as its own provider with its own circuit breaker (section 8.3). All hosts share ESPN's backend, so failover covers a host-level block but not a full ESPN outage. For NFL results, nflverse (section 6.4) is the independent second source.

| Provider | Host | Order |
|---|---|---|
| `espn_web` | `site.web.api.espn.com` | Primary |
| `espn_site` | `site.api.espn.com` | Fallback |
| `espn_cdn` | `cdn.espn.com/core/{league}/game?xhr=1&gameId={id}` | Third. Verified: `gamepackageJSON` contains `header` and a `boxscore` identical to the summary's. Unwrap it and reuse the summary parser |

Paths on the first two hosts (prefix `/apis/site/v2/sports`):

| Use | Path |
|---|---|
| Schedule and live scores | `/{sport}/{league}/scoreboard?dates=YYYYMMDD` |
| Box score and player stats | `/{sport}/{league}/summary?event={espn_event_id}` |
| Roster, for the player picker | `/{sport}/{league}/teams/{team_id}/roster` |

`{sport}/{league}` is one of `football/nfl`, `basketball/nba`, `baseball/mlb` or `hockey/nhl`.

Roster responses (verified): `athletes` is a list of groups (`offense`, `defense`, `specialTeam`, `injuredReserveOrOut`, `suspended`, `practiceSquad`), each with `items` carrying `id`, `fullName`, `position.abbreviation` and `status`. Offer every group in the player picker, with injured and suspended players last.

Request rules (enforced in `http.py`, for both web and worker):
- **User-Agent:** a fixed, honest, non-browser string: `ParlayTracker/1.0`. Never a spoofed browser string.
- **Headers:** `Accept: application/json` and gzip. Use one shared client with keep-alive, and no cookies.
- **Dates:** single `dates=YYYYMMDD` only. Never ranges.
- **Rate limits:** at most 1 request every 2 seconds per host, and at most 20 requests a minute to ESPN overall. A request over the limit is skipped until the next run, never queued.

Rules for parsers:
- **Record real responses as fixtures in `tests/fixtures/espn/` before writing any parser.** For each sport, record at least one scheduled, one in-progress, one final, one overtime and one postponed game. For the NFL, also record:
  - a halftime game and a weather-delayed game (only possible during a live game: use the recorder, section 8.3);
  - a box score with a targeted player who made zero receptions;
  - an Akamai "Access Denied" 403 body, which is HTML, not JSON.
- Parse player stats by the column `keys` array (e.g. `receivingYards`), never by position or by the display `labels`.
- Scores and stats arrive as strings (`"24"`); convert them in the parser.
- Validate the part of each response you use with a Pydantic model, so a format change raises a `schema` failure (section 8.3) instead of producing wrong values.
- The parser returns typed dataclasses. Nothing outside `espn.py` touches raw JSON.
- Treat every field as optional. A missing field is a parse error for that event only.
- Map ESPN's status to `EventStatus` by `status.type.name`, falling back to `status.type.state` (`pre`, `in`, `post`) for names you don't recognise, and log those. Verified so far:

  | ESPN `status.type` | `EventStatus` |
  |---|---|
  | `STATUS_SCHEDULED`, state `pre` | `scheduled` |
  | `STATUS_FINAL`, state `post` (overtime: period 5, detail "Final/OT") | `final` |
  | `STATUS_POSTPONED`, state `post`, `completed` false | `postponed` |
  | Halftime and end-of-period names (expected `STATUS_HALFTIME`, `STATUS_END_PERIOD`) | `break` (to verify live) |
  | Delay names (expected to contain `DELAY`) | `delayed` (to verify live) |
  | Any other state `in` | `in_progress` |

  Note that a postponed game has state `post`: never treat `post` alone as final.

Stat mapping (verified against real box scores, 2026-09-28):

| Market | Box-score group (`statistics[].name`) | Column key |
|---|---|---|
| `player_receptions` | `receiving` | `receptions` |
| `player_receiving_yards` | `receiving` | `receivingYards` |
| `player_rushing_yards` | `rushing` | `rushingYards` |
| `player_passing_yards` | `passing` | `passingYards` |
| `player_points` (NBA) | the single unnamed group | `points` |
| `player_points` (NHL) | `forwards` and `defenses` | `goals` + `assists` (there is no points column) |

NBA athletes also carry `didNotPlay` and `reason`; use them to identify a player who didn't play.

Plausibility bounds. A response with any value outside these is rejected as `implausible` (section 8.3):

| Value | Allowed range |
|---|---|
| NFL team score | 0 to 99 |
| Rushing or receiving yards | −30 to 400 |
| Passing yards | −30 to 700 |
| Receptions | 0 to 25 |

**Known trap:** the receiving table lists every **targeted** player, including those with 0 receptions, but a player who was never targeted doesn't appear at all, whether or not he played. Absence therefore means neither zero nor void. Section 7.1 settles it from snap-count evidence, or sends it to Review.

### 6.2 The Odds API: closing lines only

- The free tier gives 500 credits a month.
- A live odds call costs (number of markets) × (number of regions) credits.
- Historical odds cost 10× that, so they are not used.
- Read `x-requests-remaining` from every response and store it in `source_health.quota_remaining`. `x-requests-last` gives the cost of that call.
- Verified: `/v4/sports` and `/v4/sports/{sport}/events` cost 0 credits.
- The account had already used 49 credits this month before the build began, so budget from `x-requests-remaining`, never from a fixed monthly total.

Sport keys: `americanfootball_nfl`, `basketball_nba`, `baseball_mlb`, `icehockey_nhl`.

Market keys, kept in one dict in `resolve.py`. All NFL keys were verified with one call for a real game, which cost 14 credits (14 markets × 1 region); `player_points` and `player_points_alternate` are still to verify on an NBA or NHL game:

| Market | Main key | Alternate key |
|---|---|---|
| `game_total` | `totals` | `alternate_totals` |
| `team_total` | `team_totals` | `alternate_team_totals` |
| `alt_spread` | `spreads` | `alternate_spreads` |
| `player_receptions` | `player_receptions` | `player_receptions_alternate` |
| `player_receiving_yards` | `player_reception_yds` | `player_reception_yds_alternate` |
| `player_rushing_yards` | `player_rush_yds` | `player_rush_yds_alternate` |
| `player_passing_yards` | `player_pass_yds` | `player_pass_yds_alternate` |
| `player_points` | `player_points` | `player_points_alternate` |

Matching:
- **Events:** resolve `odds_api_event_id` once, using `/v4/sports/{sport_key}/events`. Both team names must match through the team alias table, and `commence_time` must be within 3 hours of `start_time`. Cache the ID on `events`.
- **Players:** a player-prop outcome carries the player's name in its `description` field, `name` is `Over` or `Under`, and the line is `point`. Match `description` against `legs.player_name` with rapidfuzz, requiring a score of at least 90. Otherwise there is no match.
- **Team names** are full names (e.g. `Chicago Bears`), matching ESPN's `displayName`.
- **Bookmakers** returned for an NFL game with `regions=us` (2026-09-28): `draftkings`, `fanduel`, `betmgm`, `betrivers`, `betus`, `lowvig`, `betonlineag`, `mybookieag`, `bovada`. Caesars (`williamhill_us`) was **not** returned, so Caesars legs fall through to the median of the other books (section 8.2).

### 6.3 Qwen: screenshot extraction only

Qwen is reached through **OpenRouter**, which serves Qwen's vision models behind an OpenAI-compatible API. Alibaba's own DashScope service is no longer used.

- Call it with the `openai` SDK: `OpenAI(api_key=QWEN_API_KEY, base_url=QWEN_BASE_URL)`.
- **Model:** `qwen/qwen3-vl-32b-instruct` by default. On 2026-09-28 it cost $0.104 per million input tokens and $0.416 per million output tokens, which is well under a tenth of a cent per slip, and it supports structured outputs.
  - Free alternative: `qwen/qwen3.8-27b:free`. It is limited to 20 requests a minute and 50 a day, or 1,000 a day once $10 of credit has been bought. It supports structured outputs but not plain JSON mode. Free endpoints may also be withdrawn at short notice.
  - Changing model is only a `QWEN_VISION_MODEL` setting.
- **Privacy:** some providers, especially behind free endpoints, may retain or train on inputs. Slips show stakes and possibly account details, so turn off training-permitted providers in OpenRouter's privacy settings.
- Make **one** call per screenshot:
  - `chat.completions.create(model=QWEN_VISION_MODEL, temperature=0, timeout=45, response_format={"type": "json_schema", ...})` with `ExtractedSlip.model_json_schema()` as the schema.
  - A single user message containing the image as a base64 data URI plus the text prompt.
- The prompt also includes the schema and says:
  - transcribe exactly what is on the slip, and do not guess;
  - use `null` for anything not visible;
  - give odds as signed integers;
  - return only JSON.
- Mapping sportsbook wording to `MarketType` is done by the alias table in `resolve.py`, not by another model call.
- Cost is per token: fractions of a cent per slip at this volume, but not zero. The app is reachable only over Tailscale, so nobody else can spend it.

### 6.4 nflverse: independent NFL results

nflverse publishes NFL data built from the league's own game data, independent of ESPN. It is free, and `nflreadpy` downloads it from GitHub release assets (`github.com` redirecting to `release-assets.githubusercontent.com`). It is not live; it is published overnight.

Verified on 2026-09-28:
- Sunday's (week 3) player stats and snap counts were already published by Monday afternoon.
- Across 7 games and 290 player stat lines, ESPN and nflverse agreed on every one, and every ESPN athlete ID mapped. So `verify_nfl` should rarely flag anything other than genuine stat corrections.

It is used only by the worker, for two jobs:
- **checking** every NFL leg the next day (`verify_nfl`, section 7.1);
- **settling** NFL legs when ESPN can't provide a final box score.

| Dataset (`nflreadpy`) | Used for | ESPN link |
|---|---|---|
| `load_schedules()` | Final scores (`home_score`, `away_score`, `overtime`) | `espn` column = ESPN event ID (a string) |
| `load_players()` | Player ID mapping | `espn_id` (a string) ↔ `gsis_id` ↔ `pfr_id` |
| `load_player_stats()` (weekly) | `receptions`, `receiving_yards`, `rushing_yards`, `passing_yards` | `player_id` = `gsis_id`, plus `game_id` |
| `load_snap_counts()` | Did the player take a snap? (`offense_snaps`) | `pfr_player_id` = the players table's `pfr_id`, plus `game_id` |

Rules:
- Map players only through these ID columns; never by name. A leg whose player can't be mapped stays unverified. That is shown in the UI, but not added to the Review queue.
- Load each dataset at most once per job run, and cache it in memory for that run.
- Store small slices of each dataset in `tests/fixtures/nflverse/` for tests.

---

## 7. Settlement

### 7.1 Legs

`settle_leg(market_type, line, value)` is a pure function returning `win`, `loss` or `push`:
- **Every market except `alt_spread`:** win if `value > line`, push if `value == line`, loss if `value < line`.
- **`alt_spread`:** `value` is the chosen side's margin (its score minus the opponent's). Let `x = value + line`. Win if `x > 0`, push if `x == 0`, loss if `x < 0`. In "Over" terms, the side wins by more than `-line`.

Required test table (numbers verified):

| Market | Line | Final value | Result |
|---|---|---|---|
| `player_rushing_yards` | 50.5 | 51 | win |
| `player_rushing_yards` | 50.5 | 50 | loss |
| `game_total` | 45 | 45 | push |
| `game_total` | 45 | 46 | win |
| `alt_spread` | −7.5 | margin 8 | win |
| `alt_spread` | −7.5 | margin 7 | loss |
| `alt_spread` | +3.5 | margin −3 | win |
| `alt_spread` | +3.5 | margin −4 | loss |
| `alt_spread` | −7 | margin 7 | push |

Rules for every sport:
- **A leg settles only after its event has been final for at least 10 minutes.** Live values are for display and never settle anything, because yardage can fall (negative plays, penalties, stat corrections) and margins swing both ways. The 10-minute gate gives the stat crew time to finish, and stops a single glitched "final" from settling anything.
- Final scores include overtime and extra innings.
- `settlement_source` records where the value came from: the ESPN provider that served the box score, `nflverse`, or `manual`.
- **Event postponed or cancelled, or not final 8 hours after its start:** set `needs_review`.
- **`other` legs:** set `needs_review` ("Settle manually") once their event is final.
- **The worker never changes a settled result by itself.** Any disagreement goes to Review.

**Player missing from the relevant box-score table at final.** Never void automatically, and never assume zero without evidence.
- **NFL:** the leg stays pending until `verify_nfl` runs (below). Then:
  - if nflverse has a stat line, settle from it (`settlement_source = 'nflverse'`);
  - if neither source has a stat line, but snap counts show the player took at least one snap, settle with value 0 and reason "Played, no stat". The player took part, so the prop has action;
  - if there are zero snaps, or still no snap data after the Tuesday following the game, set `needs_review` with "No stat line and no snaps: likely void".
- **NBA and NHL:** set `needs_review` with "No stat line: enter 0 or void".

**NFL verification (`verify_nfl`, daily at 10:00 ET, after nflverse's overnight publish).** For every NFL leg that settled in the last 7 days and is not yet verified, compare it with nflverse (section 6.4). For team markets compare the final scores; for player markets compare the stat.
- **Agree:** set `verified_at` and `verified_source = 'nflverse'`.
- **Disagree:** set `needs_review` with "Sources disagree: ESPN X, nflverse Y". This also catches stat corrections.
- **Still missing after the Tuesday following the game:** leave it unverified. The UI shows this, but it doesn't go into Review.

**ESPN unavailable.** If no ESPN provider has returned a final box score 6 hours after an NFL game's expected end (start + 4 hours), `verify_nfl` settles the legs from nflverse once its schedule shows a final score. Those legs stay unverified, because no second source agreed.

**Stat corrections for NBA, NHL and MLB.** 24 hours after a leg settles, the worker re-reads the ESPN box score once. If the value has changed, set `needs_review` with "Stat correction: was X, now Y".

### 7.2 Slips

Re-evaluate a slip whenever one of its legs changes. Unplaced slips use a notional stake of 1 unit.

| Legs | Slip type | Slip result | Payout (amount returned, including stake) |
|---|---|---|---|
| Any leg lost | any | loss, immediately, even if other legs are still pending | 0 |
| Some pending, none lost | any | pending | — |
| All won | any | win | stake × decimal(slip odds) |
| The leg pushed or was void | single | push / void | stake |
| All legs push or void | parlay, SGP | void | stake |
| Some push or void, the rest won | parlay, not boosted | win | stake × product of decimal(odds) over the winning legs |
| Some push or void, the rest won | SGP, or boosted | `needs_review`: "Reduced parlay: enter the payout from the sportsbook". An unplaced slip becomes void instead | entered by hand |
| Cashed out (user action) | any | `cashed_out` | amount entered |

Worked example, which must also be a test:
- A $10 parlay at −110, +120 and −120 has decimal odds of 7.70 (+670) and pays $77.00.
- If the +120 leg pushes, the odds become 1.9091 × 1.8333 = 3.50 (+250), and it pays $35.00.

Once a slip has lost, its remaining legs still settle normally, because leg results feed the selection analytics.

### 7.3 Odds maths (`core/odds.py`)

- `decimal(a)` = `1 + a/100` if `a > 0`, else `1 + 100/|a|`
- `american(d)` = `round((d − 1) × 100)` if `d ≥ 2`, else `round(−100 / (d − 1))`
- `implied(a)` = `|a| / (|a| + 100)` if `a < 0`, else `100 / (a + 100)`. So −110 is 52.38% and +150 is 40.00%
- `no_vig(p_over, p_other)` = `p_over / (p_over + p_other)`
- `wilson(wins, n, z=1.96)` gives the 95% Wilson interval. 18 wins from 30 gives 42.3%–75.4%

Port the existing test cases from `tests/test_math_calculator.py` before deleting that file.

---

## 8. Worker

### 8.1 Jobs

- Every job is registered with `coalesce=True, max_instances=1, misfire_grace_time=30`.
- Every job body is wrapped in a try/except that logs and records the error. A job never raises into the scheduler.
- When there is nothing to do, a job costs one cheap query.

An **active NFL event** is an NFL event with at least one pending leg, whose status is not `final`, `postponed` or `cancelled`, and whose start time is between 8 hours ago and 30 minutes from now. Only NFL is tracked live (section 1.1).

| Job | Runs | What it does |
|---|---|---|
| `heartbeat` | every 60 s | Upserts `source_health('worker')`, and writes each provider's breaker state and hourly counters to `source_health` |
| `capture_closing` | every 60 s | Section 8.2 |
| `poll_nfl_live` | ticks every 30 s | Makes whichever calls are due under the cadence table below. Updates event status, scores and progress key (8.3), and sets `live_value` and `live_source` on legs |
| `check_finals` | every 15 min | NBA, NHL and MLB only. From start + 2.5 h (NBA) or start + 3 h (NHL, MLB) until each event is final: one scoreboard call per sport per game day with pending legs |
| `settle` | every 5 min | Events that have been final for at least 10 minutes and still have pending legs: summary call, settle the legs (7.1), then their slips (7.2). Also flags postponed and stale events |
| `verify_nfl` | daily at 10:00 ET | Section 7.1: check NFL legs against nflverse, and settle from nflverse when ESPN couldn't |
| `recheck_settled` | every 1 h | NBA, NHL and MLB legs settled 24–25 hours ago (7.1) |
| `canary` | at startup, and daily at 09:00 ET | Fully parses the latest completed NFL game from every ESPN provider, and loads the nflverse datasets. A failure opens that provider's breaker with its failure kind (8.3), so the banner shows it before the next game day |
| `prune_samples` | daily | Keeps the last 5 failure samples per provider; deletes recordings older than 30 days |

Cadence for each active NFL event. One scoreboard call covers every NFL game that day, and it is fetched at the shortest interval any active game needs:

| Game state | Scoreboard | Box score (`summary`) |
|---|---|---|
| `scheduled`, from 30 min before kickoff | every 5 min | none |
| `in_progress` | every 30 s | every 60 s, only if the event has pending player legs |
| `break` or `delayed` | every 2 min | every 3 min |
| `final`, `postponed`, `cancelled` | stop; `settle` takes over | stop |

Worst case: a 14-game Sunday with player props in 5 games is about 7 ESPN requests a minute, well inside the limits in section 6.1.

**Single instance:** at startup the worker takes `pg_try_advisory_lock` on a dedicated connection that it holds for the life of the process. If the lock is already held, it exits. Two workers would poll twice and spend Odds API credits twice.

### 8.2 Closing-line capture

Closing lines cannot be backfilled, because historical odds are not affordable, so this is the most time-critical job.

1. Select pending, non-`other` legs with no closing line whose event starts within the next 5 minutes. Group them by event.
2. Resolve `odds_api_event_id` if it is missing (6.2).
3. **Budget check.** The cost is (number of markets) × 1 region. If `quota_remaining − cost < ODDS_API_RESERVE`, skip the call and flag the legs for manual entry.
4. Make one call: `/v4/sports/{key}/events/{id}/odds?regions=us&oddsFormat=american&markets=<main keys>`.
5. For each leg, take the first of these that exists:
   1. the exact line at the leg's sportsbook;
   2. the exact line from **one** follow-up call per event, requesting only the alternate keys still needed;
   3. the main line at the leg's sportsbook (this gives line CLV only);
   4. the median main line across US books.

   Record `closing_line`, `closing_odds`, `closing_opposite_odds` (the Under or other side at that line), `closing_source = 'odds_api'` and `closing_captured_at`.
6. If nothing is found, or the event started before capture ran (for example, because the worker was down), flag the leg in Review as "Enter closing line manually".

### 8.3 Failure handling

**HTTP.** Use one shared httpx client with `timeout=httpx.Timeout(10, connect=5)`, plus the headers and rate limits in section 6.1. Jobs never retry internally; the schedule is the retry.

**Circuit breaker: one per provider** (`espn_web`, `espn_site`, `espn_cdn`, `nflverse`, `odds_api`).
- **Closed:** normal use.
- **Open:** the provider is skipped until `open_until`.
- **Half-open:** once `open_until` passes, the next request is a trial. Success closes the breaker and resets its counters. Failure reopens it for the next duration.

A breaker always half-opens again, so no provider is ever abandoned for good. Every failure is classified:

| Response | `failure_kind` | Breaker action |
|---|---|---|
| Timeout, connection error, 5xx, or a 200 that isn't JSON or is truncated | `transient` | Open after 3 consecutive failures, for 30 s, doubling each time up to 5 min |
| 403, including Akamai's HTML "Access Denied" page | `blocked` | Open immediately for 10 min. Retrying sooner makes blocks last longer |
| 429, or any `Retry-After` header | `throttled` | Open immediately for `Retry-After`, or 5 min if there isn't one |
| 200 JSON that fails the Pydantic model | `schema` | Open for 30 min, because retrying won't fix a format change. Save the raw body |
| 200 JSON outside the plausibility bounds (6.1) | `implausible` | Reject the response, save the raw body, and open as for `schema` |
| Feed confirmed frozen (below) | `frozen` | Open for 5 min |

Breaker transitions are written to `source_health` immediately; the hourly counters are written at each heartbeat.

**ESPN router (`router.py`).**
- Each ESPN request goes to the first provider, in the order of section 6.1, whose breaker isn't open.
- If that request fails, the router tries the next provider once in the same run.
- It stays with a working fallback, and returns to the primary once the primary's breaker half-opens and its trial succeeds.
- The web process uses the same router for the game and roster pickers, with its own in-memory breakers.
- The Odds API and nflverse are single providers: same breaker rules, no fallback. Section 8.2 and section 7.1 cover what happens when they're unavailable.

**Integrity guards (`guards.py`).** Never show or store data older than what is already held.
- **Progress key per event:** (status rank, period, seconds elapsed in the period).
  - Status rank is 0 for `scheduled`; 1 for `in_progress`, `break` and `delayed`; 2 for `final`.
  - Overtime is period ≥ 5. Seconds elapsed = period length − `clock_seconds`.
- **Comparing a new response with the stored key:**
  - **Lower:** a stale cached response. Discard it and count it.
  - **Equal, with different values:** a correction. Accept it and log it.
  - **Higher:** accept it and set `last_progress_at`.
  - Score decreases are accepted (an overturned touchdown) and logged. `final` never goes back to in play.
- **Frozen feed:** the event is `in_progress` (not `break` or `delayed`) and `last_progress_at` is more than 5 minutes old.
  - Make one probe request to the next ESPN provider.
  - If the probe shows a higher key, open the current provider's breaker as `frozen` and use the probe's data.
  - If not, the game itself is probably stopped (a review, an injury). Change nothing, and let the Live card show "no change for N min" (9.4).
  - Probe at most once every 5 minutes per event.
- **Plausibility bounds:** section 6.1.

**Raw samples and recording.**
- Every `schema` or `implausible` failure saves the raw body, truncated to 1 MB, to `raw_samples` with reason `failure`. `prune_samples` keeps the last 5 per provider.
- For events listed in `RECORD_EVENT_IDS`, every response is saved with reason `recording`. That yields a full real game for the replay test (section 11).
- `python -m parlaytracker.cli export-sample <id> <path>` writes a sample out as a test fixture.

**Per-event errors.** A parse error for one event goes in `events.last_error` and does not stop other events updating.

**Visibility.** Log to stdout, where Docker collects it (`docker compose logs`). But failures must be visible in the **UI** (sections 9.2 and 9.4), not only in the logs.

---

## 9. Frontend (Streamlit)

### 9.1 Auth (Tailscale identity)

- The app is reachable only through Tailscale Serve on the laptop (section 12). Streamlit is published on `127.0.0.1` only, so no other device can reach it directly.
- Tailscale Serve adds a `Tailscale-User-Login` header (e.g. `alice@example.com`) to every request it proxies. The app reads it with `st.context.headers`.
- Every page runs a guard (`app/auth.py`):
  - **login in `ALLOWED_LOGINS`:** continue, with `logged_by` set to that login;
  - **header missing:** if `DEV_LOGIN` is set (local development only), use it; otherwise stop with "Open ParlayTracker through Tailscale";
  - **any other login:** stop with "This Tailscale account isn't allowed".
- The header is trustworthy only because the app listens on localhost behind Serve. Never expose the port any other way: no Tailscale Funnel, no router port forwarding.
- There is no password and no OAuth provider. Signing in to Tailscale (with Google, Microsoft, Apple, GitHub and so on) is the login.
- Verify at build time that the header reaches `st.context.headers` for the app's websocket session (section 15).

### 9.2 Navigation

- Use `st.navigation` pages, so only the active page runs. The pages are: Log (the default), Screenshot, Live, Analytics, Review (with the pending count in its title) and Settings.
- Every page shows a banner when any of these is true:
  - the worker heartbeat is more than 3 minutes old;
  - a provider's breaker has been open for more than 5 minutes. Name the provider and the failure kind (8.3). This includes failures found by the daily canary;
  - a provider's breaker is open with `schema`. Say "ESPN changed its format; the parser needs updating".

### 9.3 Log (Quick Add)

Target: an unplaced single can be logged in 10 seconds or less. The form lives in `components/slip_form.py` and is shared with Screenshot.

1. **Sport** (remembered), then **Game**. The game list comes from that game day's ESPN scoreboard: cached for 10 minutes, sorted by start time, with a day selector defaulting to today (US Eastern).
2. **Legs**, one row each: Market, then either Player or Side, then Line, then Odds.
   - Player is a roster dropdown with type-ahead, cached for 12 hours.
   - Side is home or away, labelled with the team names.
   - "Add leg" adds another row.
   - The slip type is inferred: one leg is a single; several legs in the same game suggest an SGP (with a toggle); anything else is a parlay.
3. **Slip odds.** A single uses the leg odds. A parlay's odds are computed from its legs and can be edited. SGP odds are entered.
4. **Sportsbook** (remembered) and **Placed?** A placed slip also needs Stake, with optional Potential payout. Then Boosted?, Tags (a multiselect with the most-used first, plus an inline "new tag" with a category) and Notes.
5. **Save** calls `services.create_slip()`. Validation errors appear next to their fields. Warnings (section 5) are shown but don't block saving.

After saving, keep the sport, game and sportsbook, and clear the leg rows.

### 9.4 Live

- Wrap only the live section in `@st.fragment(run_every=15)`.
- Show one card per pending slip that has at least one active NFL event:
  - each NFL leg shows its line, its live value, whether it is over or under right now, and the game clock or status;
  - each non-NFL leg shows "Not tracked live: settles after the game";
  - parlay cards show progress, for example "2 of 4 over, 1 lost".
- Every NFL card shows "updated N s ago · via {provider}", taken from `live_updated_at`, `events.last_polled_at` and `live_source`. While the game is in progress, it turns amber after 2 minutes and red after 5.
- If a game is `in_progress` and `last_progress_at` is more than 5 minutes old, the card also shows "No change for N min" in amber (8.3).
- When every ESPN provider's breaker is open, the page shows "Live data unavailable since HH:MM (ESPN is blocking or erroring). Results will still settle after the game."
- Below the live cards, "Settled in the last 7 days" lists each slip's result. NFL legs are marked "verified", "awaiting verification" or "unverified" (7.1).

### 9.5 Review

A queue of everything with `needs_review`, plus legs still missing a closing line after their game started. Each item shows its reason and the matching action:
- set the leg result or value;
- enter the slip payout;
- enter the closing line and odds (`closing_source = 'manual'`);
- mark the slip cashed out;
- void.

When two sources disagree, show both values side by side. Every action goes through `services`.

### 9.6 Screenshot

1. Upload a png, jpg or webp of up to 8 MB.
2. Downscale it so the longest side is at most 2000 px.
3. Call Qwen (6.3), parse the result into `ExtractedSlip`, then resolve it (below).
4. Show the **same** slip form, pre-filled, with unresolved or doubtful fields highlighted and the image alongside.

Any failure (timeout, non-JSON, an empty result) shows the same form, empty, with the message "Couldn't read this slip. Enter it manually." Nothing is saved until the user presses Save.

Resolution is deterministic and lives in `resolve.py`:
- **Sportsbook** text is matched to a sportsbook through an alias table.
- **Market** text is mapped to a `MarketType` through an alias table. For example, "Rec Yds" and "Receiving Yards" map to `player_receiving_yards`, and "Alt Spread", "Run Line" and "Puck Line" map to `alt_spread`. An Under or an unsupported market becomes an `other` leg, with the slip text as its description.
- **Event** text is matched to an ESPN event on the slip's game day, give or take a day. Matching uses team aliases built from ESPN team data (display name, short name, abbreviation, nickname).
- **Player** names are matched against the event's two rosters with rapidfuzz:
  - 90 or above: select automatically;
  - 75–89: pre-select, but highlight;
  - below 75: leave blank.

### 9.7 Settings

Manage tags (rename, merge, retire) and sportsbooks.

---

## 10. Analytics

### 10.1 Two views

**Selections: where is the edge?** This view works at leg level and is pooled across both users.
- **Unit:** every settled, non-`other` leg. If identical selections (same `selection_key()`) were logged more than once, they count once, using the earliest record.
- **Hit rate:** wins / (wins + losses), with a 95% Wilson interval.
- **Break-even:** the mean implied probability of the legs' odds.
- **Which legs count:** hit rate vs break-even, flat 1-unit ROI and CLV use only legs that have odds. SGP legs without their own odds count toward hit rate only. Show both sample sizes.
- **Closing line value (CLV)**, for legs that have a closing line. Show these as separate columns and never mix them:
  - **Price CLV (same line at close):** the no-vig closing probability (or the raw implied probability if there is no opposite price) minus the implied probability of the odds taken, in percentage points. Example: Over taken at −110 (52.38%) that closes at Over −125 / Under +105 has a no-vig close of 53.25%, so the CLV is +0.87 pp.
  - **Line CLV (line moved):** in points. For totals, team totals and player props it is closing − taken; for alt spreads it is taken − closing. Example: Over 45.5 taken, closing at 47.5, is +2.0.

**Slips: what did the money do?**
- **Placed slips:** staked, returned, P/L and ROI, broken down by slip type, leg count, sportsbook and month, plus a cumulative P/L chart.
- **Unplaced slips:** the same figures in units, labelled "if bet at 1 unit".

### 10.2 Dimensions (columns, not tags)

Group and filter by:
- sport, market, sportsbook
- slip type, leg count
- placed or unplaced, logged by
- odds band: ≤ −150, −149 to −111, −110 to +100, +101 to +150, > +150
- weekday of the game day
- start window (US Eastern): before 5pm, 5–8pm, after 8pm
- lead time from logging to start: under 1 h, 1–6 h, 6–24 h, over 24 h
- month

Tags are for context only. Suggested categories: situation, injury, weather, matchup, source. The tag filter offers "any" or "all" of the selected tags.

### 10.3 Statistical honesty

- Show the sample size (n) on every figure.
- Grey out any group where n < `MIN_SAMPLE` and label it "low sample".
- Show the interval next to every hit rate. For example, 18 wins from 30 at −110 is a 60% hit rate, but its interval (42–75%) contains the 52.4% break-even, so it is not yet evidence of an edge.
- When more than two filters are applied, show a one-line caution: the more slices you look at, the more of them will look profitable by chance.
- CLV is the headline edge indicator, because it becomes informative far sooner than ROI.

---

## 11. Testing

- **No automated test calls a real external service.** Mock HTTP with respx, using recorded fixtures. Optional live smoke tests are marked `@pytest.mark.live` and skipped by default.
- **Unit tests** (no database):
  - odds maths;
  - every row of the tables in 7.1 and 7.2;
  - CLV and the Wilson interval;
  - the ESPN and Odds API parsers against every fixture;
  - alias resolution;
  - extraction parsing against recorded Qwen responses: valid, partial, malformed, and JSON wrapped in code fences;
  - the integrity guards: every progress-key case in 8.3, the plausibility bounds, and the frozen-feed rule, including a stopped clock during a review, which must **not** switch provider;
  - the nflverse loaders and ID mapping against fixture slices, and every branch of the NFL rules in 7.1: agree, disagree, missing, snaps > 0, zero snaps, and ESPN unavailable.
- **Database tests** (real Postgres, as a CI service container or locally):
  - migrations upgrade and downgrade;
  - the constraint and validation cases below;
  - `create_slip`, cascades and the de-duplication query;
  - analytics queries on a seeded dataset, checked against hand-computed numbers.

  The constraint and validation cases, all of which must be **rejected**:

  | Rejected by the database | Rejected by `SlipIn` / `LegIn` |
  |---|---|
  | Leg or slip odds between −99 and +99 | A line that isn't a whole or half number |
  | A team market with no side | Odds between −99 and +99 |
  | A side on a non-team market | A negative Over line (not alt spread) |
  | A player market with no athlete | A team market with no side |
  | A non-`other` leg with no line | A player market with no athlete |
  | An `other` leg with no description | An `other` leg with no description |
  | An unknown market value | A non-`other` leg with no line |
  | A placed slip with no stake | A placed slip with no stake |
  | A duplicate tag (category, name) | A single with 2 legs, or leg odds ≠ slip odds |
  | A settled leg with no `settlement_source`, or a pending leg with one | A parlay with 1 leg, or a leg with no odds |
  | `verified_at` without `verified_source`, or the reverse | An SGP whose legs span two games |
  | An unknown event status, data source or failure kind | The same selection twice on one slip |
  | A raw sample whose reason isn't `failure` or `recording` | An unknown market value |

- **Worker tests:** run each job with mocked HTTP. Every job must return normally whatever the HTTP layer does.
- **Fault matrix.** For each ESPN provider, inject:
  - every row of the failure table in 8.3: a timeout, a 500, the recorded Akamai 403 HTML page, a 429 with and without `Retry-After`, truncated JSON, a changed format, and an implausible value;
  - a stale response with a lower progress key;
  - a frozen feed.

  For each one, check:
  - the failure kind and the breaker's open duration;
  - that the router moves to the next provider in the same run;
  - that `source_health` records it;
  - that other events still update.
- **Replay test.** Replay a real NFL game recorded with `RECORD_EVENT_IDS` through `poll_nfl_live`, on a fake clock. Check that:
  - live values never go backwards in progress;
  - halftime and stopped-clock periods don't cause a provider switch;
  - a 403 injected mid-game fails over, and the primary is used again once its breaker recovers;
  - the final settles correctly after the 10-minute gate.
- **Request budget.** Drive `poll_nfl_live` through a simulated 14-game Sunday and assert that ESPN requests stay within the limits in 6.1.
- **App tests:** use `streamlit.testing.v1.AppTest` for:
  - the auth guard: an allowed login, another login, and a missing header with and without `DEV_LOGIN`;
  - Log: a single, a parlay, an SGP and validation errors;
  - Screenshot: success and failure paths, with mocked extraction;
  - Review actions;
  - Analytics rendering on seeded data.
- **CI** (GitHub Actions): ruff and pytest against a Postgres service on every push.

---

## 12. Deployment (home laptop + Tailscale)

The app runs on one dedicated laptop at home. Nothing is exposed to the internet; the only way in is Tailscale.

**The laptop**
- 64-bit, at least 4 GB RAM (8 GB is comfortable), about 20 GB free disk, always plugged in. A working battery rides out short power cuts.
- Ubuntu Server 24.04 LTS.
- It never sleeps, closing the lid does nothing (`HandleLidSwitch=ignore`), and the BIOS "power on after AC loss" setting is on if there is one.

**Services (`deploy/docker-compose.yml`).** One image serves `migrate`, `web` and `worker`. Every service uses `restart: unless-stopped` except `migrate`.

| Service | Runs | Notes |
|---|---|---|
| `db` | `postgres:16` | Data in a named volume; not published outside the Compose network |
| `migrate` | `alembic upgrade head` | One-shot; must succeed before `web` and `worker` start |
| `web` | `streamlit run parlaytracker/app/main.py --server.address 0.0.0.0 --server.port 8501 --server.headless true` | Published on `127.0.0.1:8501` only |
| `worker` | `python -m parlaytracker.worker` | Exactly one (the advisory lock in 8.1 is the safety net) |

**Tailscale**
- Installed on the laptop itself (not in Docker) and on both phones, with both users in the same tailnet. The free Personal plan covers up to 6 users.
- `tailscale serve --bg 8501` publishes the app at `https://<laptop-name>.<tailnet>.ts.net`, to tailnet members only. Tailscale provides the HTTPS certificate and adds the identity headers (section 9.1).
- Never use Tailscale Funnel, which would make the app public.

**Configuration.** `/opt/parlaytracker/.env`, mode 600, owned by root, never committed:
- `DATABASE_URL` (pointing at the `db` service) and `POSTGRES_PASSWORD`;
- `DISPLAY_TZ` and `ALLOWED_LOGINS`;
- `ODDS_API_KEY` and `QWEN_API_KEY`.

`DEV_LOGIN` is never set here.

**Updates (`deploy/update.sh`, run by a systemd timer every 5 minutes)**
1. `git fetch origin main`. If nothing changed, stop.
2. `git reset --hard origin/main` in the deploy checkout, which never holds local changes.
3. `docker compose build`, then `docker compose run --rm migrate`, then `docker compose up -d`.
4. If any step fails, stop and leave the running containers untouched. Log every step to the systemd journal.

Only `main` ever runs. Nothing on GitHub can push code to the laptop, because it only pulls; this matters while the repository is public. If the repository is made private, the laptop needs a read-only deploy key.

**Backups (`deploy/backup.sh`, run nightly by a systemd timer)**
- `pg_dump -Fc` into `/var/backups/parlaytracker/`, keeping 14 days.
- Copy each dump off the laptop with `rclone` to a destination the users choose, such as a cloud drive. A backup that only lives on the laptop doesn't count.
- Document the restore procedure in `deploy/README.md`, and test it once during Phase 2.

**Setup (`deploy/setup.sh`).** Idempotent, so it's safe to re-run. It:
- installs Docker and Tailscale;
- clones the repository to `/opt/parlaytracker`;
- creates `.env` from a template, prompting for each value;
- installs the systemd timers;
- starts the services and runs `tailscale serve`.

The user can run it themselves, or a Claude session started on the laptop with `claude remote-control` can run it for them.

**Status (`deploy/status.sh`).** Prints container state, the last update, the last backup and every `source_health` row.

**When the laptop is off or offline,** the worker stops:
- closing lines for games in that window are lost for good;
- live tracking stops;
- results still settle automatically once it's back (sections 7.1 and 8.1).

The worker-heartbeat banner (9.2) shows when this has happened.

---

## 13. Build phases

The order is set by what can't be recovered later. Closing lines can never be backfilled. Results can, because ESPN keeps box scores.

### Phase 0: Reset
- Port the odds test cases, then delete `src/`, `frontend.py`, `FRONTEND_README.md`, `TESTING_GUIDE.md`, `requirements.txt` and both committed `parlaytracker.db` files.
- Add `pyproject.toml`, ruff, pytest, the CI workflow, a new `.env.example` and a README that points to this spec.
- Fix `.gitignore`: remove the stray ```` ``` ```` first line, and ignore `*.db` and `.streamlit/secrets.toml`.
- **Exit:** CI green with the ported odds tests.

### Phase 1: Core
- `config`, `models`, migration `0001` with its seed data, `schemas`, `odds.py`, `settlement.py` and `services.create_slip`.
- **Exit:** every row of the settlement tables and every constraint and validation case passes against Postgres.

### Phase 2: Logging, then deploy and start using it
- ESPN client (scoreboard and roster only) with fixtures, following the request rules in 6.1 from the start.
- Tailscale auth (9.1), and the Log, Review (manual settlement and manual closing line) and Settings pages.
- The deployment files in section 12. Install them on the laptop with a worker that only writes its heartbeat.
- **Exit:**
  - both users can log singles, parlays and SGPs from their phones over Tailscale;
  - manual settlement works;
  - data survives a laptop reboot and an automatic update;
  - a backup has been restored once to prove it works.

### Phase 3: Closing lines
- Worker skeleton: heartbeat, `source_health`, the advisory lock, and the HTTP layer with the circuit breaker (8.3).
- Odds API client, `capture_closing` and the health banner.
- **Exit:** fixture tests pass; on one real game day, every eligible leg got either a closing line or a manual-entry flag; credit use matches the estimate.

### Phase 4: Auto-settlement
- ESPN summary parser, and the ESPN router with host failover, integrity guards and raw samples (8.3).
- The `check_finals`, `settle`, `recheck_settled`, `canary` and `prune_samples` jobs, and the Review reasons.
- nflverse loaders and `verify_nfl` (6.4, 7.1).
- A one-off backfill that settles everything logged since Phase 2.
- **Exit:**
  - the fault-matrix tests pass;
  - one real weekend auto-settled, with every result matching what the sportsbook paid;
  - every NFL leg from that weekend is either verified against nflverse or has a Review item explaining why not.

### Phase 5: Analytics
- Section 10 in full, built on seeded data first.
- **Exit:** analytics tests match the hand-computed numbers; every figure shows n; low-sample groups are greyed out.

### Phase 6: Screenshot extraction
- `extraction.py`, `resolve.py` and the Screenshot page.
- **Exit:** 10 real slips (singles, parlays and SGPs, from at least 2 sportsbooks) are processed, with their Qwen responses saved as fixtures. Most fields pre-fill correctly, and every failure path lands on the manual form.

### Phase 7: Live tracking
- `poll_nfl_live` with the adaptive cadence (8.1), frozen-feed detection (8.3) and the Live page (9.4).
- Record one real game with `RECORD_EVENT_IDS`, and add the replay and request-budget tests (section 11).
- **Exit:** one live NFL game tracked end to end, and:
  - blocking `site.web.api.espn.com` mid-game switches to `site.api.espn.com` within one run;
  - blocking every ESPN host turns the cards red within 5 minutes and shows the "Live data unavailable" message;
  - tracking recovers without a restart once the hosts are unblocked.

---

## 14. Non-negotiable constraints

1. A slip reaches the database only through `services.create_slip()` and `SlipIn`. AI output is never written directly.
2. Nothing settles from a live value; settlement requires an event that has been final for at least 10 minutes.
3. Nothing is voided or set to zero automatically. Anything ambiguous goes to Review.
4. Every outbound request has a timeout. No job can crash the worker. Every failure is visible in the UI.
5. Timestamps are stored in UTC.
6. Only free data sources, and no scraping. Odds API spending is budgeted and keeps a reserve.
7. Qwen models only, reached through OpenRouter; no Anthropic models.
8. Streamlit only: no separate API server, no JavaScript frontend, and data is ingested by polling.
9. Exactly one worker instance.
10. ESPN requests follow section 6.1: an honest User-Agent, single dates and the rate limits. A blocked or throttled provider is backed off, never hammered.
11. The app is reachable only over Tailscale. Never expose it publicly: no Funnel, no port forwarding.

---

## 15. Verification status

**Verified on 2026-09-28**, with the details in the sections named:
- **ESPN:**
  - late games belong to their US Eastern date (3);
  - box-score column keys for the NFL, NBA and NHL, and the roster shape (6.1);
  - `cdn.espn.com` wraps the same box score as the summary (6.1);
  - `site.api.espn.com` is blocked from a cloud server (6.1);
  - the final, overtime and postponed statuses (6.1);
  - targeted players with 0 receptions are listed (6.1).
- **The Odds API:** the sport keys, all NFL market keys, the free events endpoint, and the bookmaker keys (6.2).
- **nflverse:** ID columns, dataset columns, next-day publishing, and 290 of 290 stat lines agreeing with ESPN (6.4).
- **Qwen:** DashScope replaced by OpenRouter; model availability and pricing (6.3).

**Still to verify:**
- **ESPN:**
  - halftime, end-of-period and delay status names, and the live `period`/`clock` fields. These need a live game: record one with `RECORD_EVENT_IDS` (6.1);
  - whether `site.api.espn.com` also blocks the laptop's home connection (the canary shows it on first start).
- **The Odds API:** `player_points` and `player_points_alternate` on an NBA or NHL game, and whether Caesars (`williamhill_us`) appears for any game.
- **Tailscale:** that `Tailscale-User-Login` reaches Streamlit's `st.context.headers` (9.1).
- **Qwen via OpenRouter:** extraction quality on real slips (Phase 6).

---

## 16. Running cost (approximate)

| Item | Cost |
|---|---|
| ESPN | Free |
| nflverse | Free |
| The Odds API | Free (500 credits a month) |
| Qwen via OpenRouter | Well under a tenth of a cent per screenshot, from prepaid credit |
| Laptop | Electricity only |
| Postgres | Free, on the laptop |
| Tailscale | Free (Personal plan) |

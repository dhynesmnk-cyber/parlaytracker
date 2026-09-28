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
| Users | Two, both signed in. Analytics are pooled; every slip records who logged it |
| Hosting | A small always-on budget is accepted: web process, worker process and Postgres |
| AI | Qwen models via Alibaba Cloud Model Studio only. No Anthropic models |
| Frontend | Streamlit only |
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
browser ──▶ web (Streamlit) ──▶ Qwen vision API      (screenshot upload only)
                 │           ──▶ ESPN                 (game and roster pickers, cached)
                 ▼
            PostgreSQL ◀────── worker (APScheduler) ──▶ ESPN          (scores, box scores)
                                                    ──▶ The Odds API  (closing lines)
```

- There are two processes and one database. The processes share nothing except Postgres.
- **web** is the UI. It may sleep when nobody is using it.
- **worker** runs every background job. It is always on and holds no state except in-memory backoff timers, so after a restart it rebuilds everything from the database.
- There is no FastAPI and no other service.

### 2.1 Stack

- Python 3.12
- Streamlit ≥ 1.42, installed as `streamlit[auth]`. The features used are `st.navigation`, `st.fragment(run_every=...)` and `st.login`
- SQLAlchemy 2.0 (typed ORM), Alembic, psycopg 3
- PostgreSQL 15 or newer, from any managed provider. The code only knows `DATABASE_URL`
- APScheduler 3.x, pinned `<4` because 4.x has a different API
- httpx for all outbound HTTP
- The `openai` SDK, used for Qwen's OpenAI-compatible endpoint
- rapidfuzz, pandas, plotly, Pillow, pydantic ≥ 2, pydantic-settings
- Dev: pytest, respx, ruff
- **Not used:** FastAPI, streamlit-autorefresh, nfl_data_py, nba_api, Playwright

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
    http.py          # shared httpx client, timeouts, per-source backoff (section 8.3)
    espn.py          # client + parsers returning typed dataclasses
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
migrations/          # Alembic
scripts/start-web.sh # writes .streamlit/secrets.toml from env, then execs streamlit
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
| `DASHSCOPE_API_KEY` | from phase 6 | Must be issued in the same region as `DASHSCOPE_BASE_URL` |
| `DASHSCOPE_BASE_URL` | no | Default `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `QWEN_VISION_MODEL` | no | Default `qwen-vl-max`. Model names change, so check Model Studio |
| `MIN_SAMPLE` | no | Default 30 (section 10.3) |
| Auth values | yes | Section 9.1 |

- Every timestamp is stored in UTC (`timestamptz`) and converted to `DISPLAY_TZ` only in the UI.
- A "game day" for ESPN scoreboard requests is the US Eastern calendar date, so a Sunday-night NFL game belongs to Sunday. Confirm this against a recorded fixture (section 15).

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
- Tags attach to legs and are for subjective context only. Section 10.2 lists the dimensions that already exist as columns; these must never be tagged.

The code below has been run against PostgreSQL 16 (and SQLite): every valid case is accepted, and every constraint and validation case listed in section 11 is rejected.

```python
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Enum, ForeignKey, MetaData, Numeric,
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
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


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
    final_at: Mapped[datetime | None] = mapped_column(TZ)
    last_polled_at: Mapped[datetime | None] = mapped_column(TZ)
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
    final_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    settled_at: Mapped[datetime | None] = mapped_column(TZ)
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
    )


class SourceHealth(Base):
    __tablename__ = "source_health"
    source: Mapped[str] = mapped_column(String(30), primary_key=True)  # "espn", "odds_api"
    last_success_at: Mapped[datetime | None] = mapped_column(TZ)
    last_failure_at: Mapped[datetime | None] = mapped_column(TZ)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    quota_remaining: Mapped[int | None]  # Odds API x-requests-remaining
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

This API is unofficial, free and needs no key. One parser covers all four sports.

Base URL: `https://site.api.espn.com/apis/site/v2/sports`

| Use | Path |
|---|---|
| Schedule and live scores | `/{sport}/{league}/scoreboard?dates=YYYYMMDD` |
| Box score and player stats | `/{sport}/{league}/summary?event={espn_event_id}` |
| Roster, for the player picker | `/{sport}/{league}/teams/{team_id}/roster` |

`{sport}/{league}` is one of `football/nfl`, `basketball/nba`, `baseball/mlb` or `hockey/nhl`.

Rules:
- **Record real responses as fixtures in `tests/fixtures/espn/` before writing any parser.** For each sport, record at least one scheduled, one in-progress, one final, one overtime and one postponed game. Also record an NFL box score in which a player who played has zero receptions.
- Parse player stats by the column `keys` or `labels` arrays, never by position.
- The parser returns typed dataclasses. Nothing outside `espn.py` touches raw JSON.
- Treat every field as optional. A missing field is a parse error for that event only.

Stat mapping. Confirm each column against the fixtures:

| Market | Box-score group | Column |
|---|---|---|
| `player_receptions` | receiving | REC |
| `player_receiving_yards` | receiving | YDS |
| `player_rushing_yards` | rushing | YDS |
| `player_passing_yards` | passing | YDS |
| `player_points` (NBA) | player stats | PTS |
| `player_points` (NHL) | skater stats | G + A, or PTS if present |

**Known trap:** an NFL player who played but made no receptions does not appear in the receiving table at all. Absence therefore means neither zero nor void; the leg goes to Review (section 7.1).

### 6.2 The Odds API: closing lines only

- The free tier gives 500 credits a month.
- A live odds call costs (number of markets) × (number of regions) credits.
- Historical odds cost 10× that, so they are not used.
- Read `x-requests-remaining` from every response and store it in `source_health.quota_remaining`.

Sport keys: `americanfootball_nfl`, `basketball_nba`, `baseball_mlb`, `icehockey_nhl`.

Market keys. Verify them against the Odds API markets list and keep them in one dict in `resolve.py`:

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
- **Players:** a player-prop outcome carries the player's name in its `description` field. Match it against `legs.player_name` with rapidfuzz, requiring a score of at least 90. Otherwise there is no match.

### 6.3 Qwen: screenshot extraction only

- Call the OpenAI-compatible endpoint with the `openai` SDK: `OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)`.
- Make **one** call per screenshot:
  - `chat.completions.create(model=QWEN_VISION_MODEL, temperature=0, timeout=45, ...)`
  - A single user message containing the image as a base64 data URI plus the text prompt.
- The prompt includes `ExtractedSlip.model_json_schema()` and says:
  - transcribe exactly what is on the slip, and do not guess;
  - use `null` for anything not visible;
  - give odds as signed integers;
  - return only JSON.
- Mapping sportsbook wording to `MarketType` is done by the alias table in `resolve.py`, not by another model call.
- Cost is per token: fractions of a cent per slip at this volume, but not zero. The upload page sits behind login, so nobody else can spend it.

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

Rules:
- **A leg settles only when its event is final.** Live values are for display and never settle anything, because yardage can fall (negative plays, penalties, stat corrections) and margins swing both ways.
- Final scores include overtime and extra innings.
- **Player missing from the relevant box-score table at final:** set `needs_review` with the reason "No stat line: enter 0 or void". Never void automatically, and never assume zero.
- **Event postponed or cancelled, or not final 8 hours after its start:** set `needs_review`.
- **`other` legs:** set `needs_review` ("Settle manually") once their event is final.
- **Stat corrections:** 24 hours after a leg settles, the worker re-reads the box score once. If the value has changed, set `needs_review` with "Stat correction: was X, now Y". The worker never changes a settled result by itself.

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

An **active event** has status `scheduled` or `in_progress`, a start time between 8 hours ago and 10 minutes from now, and at least one pending leg.

| Job | Every | What it does |
|---|---|---|
| `heartbeat` | 60 s | Upserts `source_health('worker').last_success_at` |
| `capture_closing` | 60 s | Section 8.2 |
| `poll_scoreboards` | 30 s | One ESPN scoreboard call per (sport, game day) that has active events. Updates status and scores, and sets `live_value` on `game_total`, `team_total` and `alt_spread` legs |
| `poll_player_stats` | 60 s | For each in-progress event with pending player legs, one summary call, then sets `live_value` |
| `settle` | 5 min | For final events with pending legs: summary call, settle the legs (7.1), then settle their slips (7.2). Also flags postponed and stale events |
| `recheck_settled` | 1 h | Re-reads legs settled 24–25 hours ago (7.1) |

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

- **HTTP:** use one shared httpx client with `timeout=httpx.Timeout(10, connect=5)` and a descriptive User-Agent. Jobs never retry internally; the schedule is the retry.
- **Backoff is per source** (`espn`, `odds_api`), not per game. After the nth consecutive failure, skip that source for `min(30 s × 2^(n−1), 5 min)`. Reset on success. Never stop permanently while events are active.
- **Recording:** record every success and failure in `source_health`. A parse error for one event goes in `events.last_error` and does not stop other events updating.
- **Visibility:** log to stdout, where Fly collects it. But failures must be visible in the **UI** (sections 9.2 and 9.4), not only in the logs.

---

## 9. Frontend (Streamlit)

### 9.1 Auth

- Use `st.login` with an OIDC provider (Google). Configure it in the `[auth]` block of `secrets.toml`, alongside an `allowed_emails` list containing the two users.
- Every page runs a guard:
  - not logged in: show a login button;
  - logged in but not on the allowed list: stop.
- `logged_by` is `st.user.email`.
- Streamlit reads auth configuration from `.streamlit/secrets.toml`, not from environment variables. `scripts/start-web.sh` therefore writes that file from Fly secrets at startup. Never commit it.

### 9.2 Navigation

- Use `st.navigation` pages, so only the active page runs. The pages are: Log (the default), Screenshot, Live, Analytics, Review (with the pending count in its title) and Settings.
- Every page shows a banner if the worker heartbeat is more than 3 minutes old, or if any source has been failing for more than 5 minutes.

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
- Show one card per pending slip that has at least one active event:
  - each leg shows its line, its live value, whether it is over or under right now, and the game clock or status;
  - parlay cards show progress, for example "2 of 4 over, 1 lost".
- Every card shows "updated N s ago", taken from `live_updated_at` and `events.last_polled_at`. While the game is in progress, it turns amber after 2 minutes and red after 5.

### 9.5 Review

A queue of everything with `needs_review`, plus legs still missing a closing line after their game started. Each item shows its reason and the matching action:
- set the leg result or value;
- enter the slip payout;
- enter the closing line and odds (`closing_source = 'manual'`);
- mark the slip cashed out;
- void.

Every action goes through `services`.

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
  - extraction parsing against recorded Qwen responses: valid, partial, malformed, and JSON wrapped in code fences.
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
  | | A parlay with 1 leg, or a leg with no odds |
  | | An SGP whose legs span two games |
  | | The same selection twice on one slip |
  | | An unknown market value |

- **Worker tests:** run each job with mocked HTTP, then inject failures (timeout, HTTP 500, malformed JSON, missing fields). Check that the job returns, backoff advances, `source_health` records the error and other events still update.
- **App tests:** use `streamlit.testing.v1.AppTest` for:
  - Log: a single, a parlay, an SGP and validation errors;
  - Screenshot: success and failure paths, with mocked extraction;
  - Review actions;
  - Analytics rendering on seeded data.
- **CI** (GitHub Actions): ruff and pytest against a Postgres service on every push.

---

## 12. Deployment (Fly.io)

```toml
app = "parlaytracker"
primary_region = "ams"

[processes]
  web = "./scripts/start-web.sh"          # writes secrets.toml, then: streamlit run parlaytracker/app/main.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
  worker = "python -m parlaytracker.worker"

[deploy]
  release_command = "alembic upgrade head"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0
  processes = ["web"]

  [[http_service.checks]]
    method = "GET"
    path = "/_stcore/health"
    interval = "30s"
    timeout = "5s"
    grace_period = "20s"

[[vm]]
  processes = ["web"]
  memory = "512mb"

[[vm]]
  processes = ["worker"]
  memory = "256mb"
```

- The worker has no HTTP service, so Fly never auto-stops it. Run exactly one worker machine (`fly scale count worker=1`); the advisory lock (8.1) is the safety net.
- The web process may sleep. No data capture depends on it.
- Secrets: `DATABASE_URL`, `DISPLAY_TZ`, `ODDS_API_KEY`, `DASHSCOPE_API_KEY` and the auth values.
- Postgres can come from any managed provider running 15 or newer.
- This fixes four problems in the current `fly.toml` and `Dockerfile`:
  - `memory` and `memory_mb` are set to conflicting values;
  - Streamlit listens on 8501 while `internal_port` is 8080;
  - there is no worker;
  - auto-stop would kill background jobs.

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
- ESPN client (scoreboard and roster only) with fixtures.
- Auth, and the Log, Review (manual settlement and manual closing line) and Settings pages.
- Deploy with web and an empty worker.
- **Exit:** both users can log singles, parlays and SGPs from their phones; manual settlement works; data survives a redeploy.

### Phase 3: Closing lines
- Worker skeleton: heartbeat, backoff, `source_health` and the advisory lock.
- Odds API client, `capture_closing` and the health banner.
- **Exit:** fixture tests pass; on one real game day, every eligible leg got either a closing line or a manual-entry flag; credit use matches the estimate.

### Phase 4: Auto-settlement
- ESPN summary parser, the `settle` and `recheck_settled` jobs, and the Review reasons.
- A one-off backfill that settles everything logged since Phase 2.
- **Exit:** one real weekend auto-settled, with every result matching what the sportsbook paid.

### Phase 5: Analytics
- Section 10 in full, built on seeded data first.
- **Exit:** analytics tests match the hand-computed numbers; every figure shows n; low-sample groups are greyed out.

### Phase 6: Screenshot extraction
- `extraction.py`, `resolve.py` and the Screenshot page.
- **Exit:** 10 real slips (singles, parlays and SGPs, from at least 2 sportsbooks) are processed, with their Qwen responses saved as fixtures. Most fields pre-fill correctly, and every failure path lands on the manual form.

### Phase 7: Live tracking
- `poll_scoreboards` live values, `poll_player_stats` and the Live page.
- **Exit:** one live game tracked end to end. Blocking ESPN mid-game turns the cards red within 5 minutes, and tracking recovers without a restart.

---

## 14. Non-negotiable constraints

1. A slip reaches the database only through `services.create_slip()` and `SlipIn`. AI output is never written directly.
2. Nothing settles from a live value; settlement requires a final event.
3. Nothing is voided or set to zero automatically. Anything ambiguous goes to Review.
4. Every outbound request has a timeout. No job can crash the worker. Every failure is visible in the UI.
5. Timestamps are stored in UTC.
6. Only free data sources, and no scraping. Odds API spending is budgeted and keeps a reserve.
7. Qwen models only; no Anthropic models.
8. Streamlit only: no separate API server, no JavaScript frontend, and data is ingested by polling.
9. Exactly one worker instance.

---

## 15. Verify at build time

This spec could not confirm the following. Check each one and update the spec:

- **ESPN:**
  - which date the scoreboard's `dates` parameter uses for late games;
  - the box-score column keys for each sport, especially NHL points;
  - the shape of the roster endpoint.
- **The Odds API:**
  - the market keys in 6.2, especially `alternate_team_totals` and the `*_alternate` player keys;
  - the credit cost of the events endpoint;
  - the bookmaker keys for the seeded sportsbooks.
- **Qwen:**
  - the current vision model name in your region;
  - whether it accepts `response_format={"type": "json_object"}` (use it if so).

---

## 16. Running cost (approximate)

| Item | Cost |
|---|---|
| ESPN | Free |
| The Odds API | Free (500 credits a month) |
| Qwen | Fractions of a cent per screenshot |
| Fly.io | Two small machines, with the worker always on and web sleeping: a few dollars a month |
| Postgres | Depends on the provider |

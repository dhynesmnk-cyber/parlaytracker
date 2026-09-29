"""nflverse: an independent source of NFL results (SPEC.md section 6.4).

Used only by the worker, to check NFL legs the next day and to settle them when ESPN can't.
Players map only through the ID columns (ESPN id -> gsis_id -> stat lines; -> pfr_id -> snap
counts), never by name. Each dataset is loaded at most once per `NflverseData` (one per job
run), and every load goes through the `nflverse` circuit breaker.
"""
import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from parlaytracker.core.models import FailureKind, MarketType
from parlaytracker.ingest.router import Breakers

log = logging.getLogger("parlaytracker.nflverse")

SOURCE = "nflverse"

# Which weekly-stats column settles which market.
STAT_COLUMNS: dict[MarketType, str] = {
    MarketType.PLAYER_RECEPTIONS: "receptions",
    MarketType.PLAYER_RECEIVING_YARDS: "receiving_yards",
    MarketType.PLAYER_RUSHING_YARDS: "rushing_yards",
    MarketType.PLAYER_PASSING_YARDS: "passing_yards",
}

# The columns each dataset must have; a missing one is a format change (`schema`).
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "schedules": {"game_id", "espn", "home_score", "away_score", "overtime"},
    "players": {"gsis_id", "pfr_id", "espn_id"},
    "player_stats": {"player_id", "game_id", *STAT_COLUMNS.values()},
    "snap_counts": {"game_id", "pfr_player_id", "offense_snaps"},
}


class NflverseError(Exception):
    """A dataset couldn't be loaded, or isn't shaped as expected."""

    def __init__(self, dataset: str, kind: FailureKind, detail: str):
        super().__init__(f"nflverse {dataset}: {detail}")
        self.dataset = dataset
        self.kind = kind


def nfl_season(start_time: datetime) -> int:
    """The season a game belongs to: September to February all count as the starting year."""
    return start_time.year if start_time.month >= 3 else start_time.year - 1


def default_loader(dataset: str, season: int) -> Any:
    """Download a dataset with nflreadpy. Returns a polars DataFrame."""
    import nflreadpy

    match dataset:
        case "schedules":
            return nflreadpy.load_schedules([season])
        case "players":
            return nflreadpy.load_players()
        case "player_stats":
            return nflreadpy.load_player_stats([season])
        case "snap_counts":
            return nflreadpy.load_snap_counts([season])
    raise ValueError(dataset)


class NflverseData:
    """Lookups over the four datasets, loaded lazily and cached for the life of the object."""

    def __init__(self, breakers: Breakers,
                 loader: Callable[[str, int], Any] = default_loader):
        self._breakers = breakers
        self._loader = loader
        self._frames: dict[tuple[str, int], Any] = {}
        self._indexes: dict[tuple[str, int], dict] = {}

    # --- loading -----------------------------------------------------------------------------

    def load(self, dataset: str, season: int) -> Any:
        key = (dataset, 0 if dataset == "players" else season)
        if key in self._frames:
            return self._frames[key]
        self._breakers.allow(SOURCE)  # raises ProviderOpen
        try:
            frame = self._loader(dataset, season)
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"[:300]
            self._breakers.failure(SOURCE, FailureKind.TRANSIENT, detail)
            raise NflverseError(dataset, FailureKind.TRANSIENT, detail) from e
        missing = REQUIRED_COLUMNS[dataset] - set(frame.columns)
        if missing:
            detail = f"missing columns {sorted(missing)}"
            self._breakers.failure(SOURCE, FailureKind.SCHEMA, detail)
            raise NflverseError(dataset, FailureKind.SCHEMA, detail)
        self._breakers.success(SOURCE)
        self._frames[key] = frame
        return frame

    def warm(self, season: int) -> None:
        """Load everything (the canary): a failure opens the breaker with its kind."""
        for dataset in REQUIRED_COLUMNS:
            self.load(dataset, season)

    def _index(self, dataset: str, season: int, key_columns: tuple[str, ...]) -> dict:
        cache_key = (dataset, season)
        if cache_key not in self._indexes:
            frame = self.load(dataset, season)
            self._indexes[cache_key] = {
                tuple(row[c] for c in key_columns): row for row in frame.to_dicts()}
        return self._indexes[cache_key]

    # --- lookups -----------------------------------------------------------------------------

    def _game(self, season: int, espn_event_id: str) -> dict | None:
        return self._index("schedules", season, ("espn",)).get((str(espn_event_id),))

    def final_score(self, season: int, espn_event_id: str) -> tuple[int, int] | None:
        """(home, away) once nflverse has the game's final score; None until then."""
        game = self._game(season, espn_event_id)
        if game is None or game["home_score"] is None or game["away_score"] is None:
            return None
        return int(game["home_score"]), int(game["away_score"])

    def _player(self, espn_athlete_id: str) -> dict | None:
        return self._index("players", 0, ("espn_id",)).get((str(espn_athlete_id),))

    def player_mapped(self, espn_athlete_id: str) -> bool:
        return self._player(espn_athlete_id) is not None

    def stat_value(self, season: int, espn_event_id: str, espn_athlete_id: str,
                   market: MarketType) -> Decimal | None:
        """The player's stat in that game, or None: no such game, player not mapped, or no
        stat line (the player isn't in the weekly stats for that game)."""
        game, player = self._game(season, espn_event_id), self._player(espn_athlete_id)
        if game is None or player is None or player["gsis_id"] is None:
            return None
        row = self._index("player_stats", season, ("game_id", "player_id")).get(
            (game["game_id"], player["gsis_id"]))
        value = None if row is None else row.get(STAT_COLUMNS[market])
        if value is None or value != value:  # None or NaN
            return None
        return Decimal(str(value))

    def offense_snaps(self, season: int, espn_event_id: str,
                      espn_athlete_id: str) -> float | None:
        """Offensive snaps, or None when there is no snap-count row (not published, or the
        player isn't mapped)."""
        game, player = self._game(season, espn_event_id), self._player(espn_athlete_id)
        if game is None or player is None or player["pfr_id"] is None:
            return None
        row = self._index("snap_counts", season, ("game_id", "pfr_player_id")).get(
            (game["game_id"], player["pfr_id"]))
        snaps = None if row is None else row.get("offense_snaps")
        return None if snaps is None or snaps != snaps else float(snaps)

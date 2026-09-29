"""Shared by the slip-import tests: a CSV builder and the recorded game's rosters."""
from parlaytracker.ingest import espn, slip_import

def player(athlete_id: str, name: str) -> espn.RosterPlayer:
    return espn.RosterPlayer(athlete_id, name, "", "offense", False)


ROSTERS = {  # ARI (22) and SF (25) with the ESPN ids in the recorded box score
    "22": [player("4361307", "Trey McBride"), player("2578570", "Jacoby Brissett"),
           player("4360761", "Michael Wilson")],
    "25": [player("3040151", "George Kittle"), player("4363538", "Chad Ryland"),
           player("3117251", "Christian McCaffrey"), player("4034949", "Eddy Pineiro")],
}


HEADER = ",".join(slip_import.COLUMNS)


def row(**over) -> dict[str, str]:
    base = dict(slip_id="111", bet_type="sgp_parlay", leg_count="2", odds_american="+450",
                boost_pct="", status="lost", wager="40", paid="", matchup="Cardinals vs 49ers",
                game_start_iso="2026-09-27T16:05:00-04:00",
                placed_at_iso="2026-09-27T12:00:00-04:00", bookmaker="Hard Rock", leg_seq="1",
                player="Trey McBride", market="receptions", line="9.5", result="lost",
                raw_text="TREY MCBRIDE - RECEPTIONS")
    return {**base, **over}


def csv_text(rows: list[dict[str, str]]) -> str:
    lines = [HEADER]
    for r in rows:
        lines.append(",".join(r.get(c, "") for c in slip_import.COLUMNS))
    return "\n".join(lines)


def two_legs(**shared) -> list[dict[str, str]]:
    return [row(**shared),
            row(leg_seq="2", player="George Kittle", market="touchdowns", line="0.5",
                result="won", raw_text="GEORGE KITTLE - ANYTIME TD", **shared)]

"""Four more NFL player markets: pass completions, touchdowns, interceptions, field goals.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""
from collections.abc import Sequence

from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ('game_total', 'team_total', 'alt_spread', 'player_receptions', 'player_receiving_yards',
       'player_rushing_yards', 'player_passing_yards', 'player_points', 'other')
NEW = ('game_total', 'team_total', 'alt_spread', 'player_receptions', 'player_receiving_yards',
       'player_rushing_yards', 'player_passing_yards', 'player_pass_completions',
       'player_touchdowns', 'player_interceptions', 'player_field_goals', 'player_points',
       'other')


def _swap(values: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{v}'" for v in values)
    op.drop_constraint('market_type', 'legs', type_='check')
    op.create_check_constraint('market_type', 'legs', f"market_type IN ({listed})")


def upgrade() -> None:
    _swap(NEW)


def downgrade() -> None:
    op.execute("UPDATE legs SET market_type = 'other', description = COALESCE(description, "
               "'(market removed by downgrade)') WHERE market_type NOT IN "
               f"({', '.join(repr(v) for v in OLD)})")
    _swap(OLD)

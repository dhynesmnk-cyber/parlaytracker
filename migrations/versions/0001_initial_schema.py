"""Initial schema (SPEC.md section 4) and seed sportsbooks.

Revision ID: 0001
Revises: 
Create Date: 2026-09-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sport', sa.Enum('nfl', 'nba', 'mlb', 'nhl', name='sport', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('espn_event_id', sa.String(length=20), nullable=False),
    sa.Column('odds_api_event_id', sa.String(length=64), nullable=True),
    sa.Column('home_team', sa.String(length=60), nullable=False),
    sa.Column('away_team', sa.String(length=60), nullable=False),
    sa.Column('home_espn_team_id', sa.String(length=10), nullable=False),
    sa.Column('away_espn_team_id', sa.String(length=10), nullable=False),
    sa.Column('start_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Enum('scheduled', 'in_progress', 'break', 'delayed', 'final', 'postponed', 'cancelled', name='event_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('home_score', sa.Integer(), nullable=True),
    sa.Column('away_score', sa.Integer(), nullable=True),
    sa.Column('period', sa.Integer(), nullable=True),
    sa.Column('clock_seconds', sa.Integer(), nullable=True),
    sa.Column('final_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_progress_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events')),
    sa.UniqueConstraint('espn_event_id', name=op.f('uq_events_espn_event_id'))
    )
    op.create_table('raw_samples',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('espn_event_id', sa.String(length=20), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("reason IN ('failure', 'recording')", name=op.f('ck_raw_samples_reason_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_samples'))
    )
    op.create_index('ix_raw_samples_source_fetched_at', 'raw_samples', ['source', 'fetched_at'], unique=False)
    op.create_table('source_health',
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('state', sa.Enum('ok', 'degraded', 'open', name='health_state', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('failure_kind', sa.Enum('transient', 'blocked', 'throttled', 'schema', 'implausible', 'frozen', name='failure_kind', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('open_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('requests_last_hour', sa.Integer(), nullable=False),
    sa.Column('errors_last_hour', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('quota_remaining', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('source', name=op.f('pk_source_health'))
    )
    sportsbooks = op.create_table('sportsbooks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('odds_api_key', sa.String(length=50), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sportsbooks')),
    sa.UniqueConstraint('name', name=op.f('uq_sportsbooks_name'))
    )
    # Odds API bookmaker keys: verify against the Odds API list (SPEC.md section 15).
    op.bulk_insert(sportsbooks, [
        {'name': 'DraftKings', 'odds_api_key': 'draftkings'},
        {'name': 'FanDuel', 'odds_api_key': 'fanduel'},
        {'name': 'BetMGM', 'odds_api_key': 'betmgm'},
        {'name': 'Caesars', 'odds_api_key': 'williamhill_us'},
    ])
    op.create_table('tags',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tags')),
    sa.UniqueConstraint('category', 'name', name='uq_tags_category_name')
    )
    op.create_table('slips',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('logged_by', sa.String(length=255), nullable=False),
    sa.Column('is_placed', sa.Boolean(), nullable=False),
    sa.Column('slip_type', sa.Enum('single', 'parlay', 'sgp', name='slip_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('sportsbook_id', sa.Integer(), nullable=False),
    sa.Column('american_odds', sa.Integer(), nullable=False),
    sa.Column('boosted', sa.Boolean(), nullable=False),
    sa.Column('stake', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('potential_payout', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('status', sa.Enum('pending', 'win', 'loss', 'push', 'void', 'cashed_out', name='slip_status', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('payout', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('needs_review', sa.Boolean(), nullable=False),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.Column('source', sa.Enum('quick_add', 'screenshot', name='entry_source', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('NOT is_placed OR stake IS NOT NULL', name=op.f('ck_slips_placed_has_stake')),
    sa.CheckConstraint('american_odds >= 100 OR american_odds <= -100', name=op.f('ck_slips_odds_valid')),
    sa.ForeignKeyConstraint(['sportsbook_id'], ['sportsbooks.id'], name=op.f('fk_slips_sportsbook_id_sportsbooks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_slips'))
    )
    op.create_table('legs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slip_id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('market_type', sa.Enum('game_total', 'team_total', 'alt_spread', 'player_receptions', 'player_receiving_yards', 'player_rushing_yards', 'player_passing_yards', 'player_points', 'other', name='market_type', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('side', sa.Enum('home', 'away', name='team_side', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('espn_athlete_id', sa.String(length=20), nullable=True),
    sa.Column('player_name', sa.String(length=100), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('line', sa.Numeric(precision=6, scale=1), nullable=True),
    sa.Column('american_odds', sa.Integer(), nullable=True),
    sa.Column('result', sa.Enum('pending', 'win', 'loss', 'push', 'void', name='leg_result', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('live_value', sa.Numeric(precision=8, scale=1), nullable=True),
    sa.Column('live_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('live_source', sa.Enum('espn_web', 'espn_site', 'espn_cdn', 'nflverse', 'manual', name='live_source', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('final_value', sa.Numeric(precision=8, scale=1), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settlement_source', sa.Enum('espn_web', 'espn_site', 'espn_cdn', 'nflverse', 'manual', name='settlement_source', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_source', sa.Enum('espn_web', 'espn_site', 'espn_cdn', 'nflverse', 'manual', name='verified_source', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('closing_line', sa.Numeric(precision=6, scale=1), nullable=True),
    sa.Column('closing_odds', sa.Integer(), nullable=True),
    sa.Column('closing_opposite_odds', sa.Integer(), nullable=True),
    sa.Column('closing_source', sa.Enum('odds_api', 'manual', name='closing_source', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('closing_captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('needs_review', sa.Boolean(), nullable=False),
    sa.Column('review_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("(market_type IN ('team_total', 'alt_spread')) = (side IS NOT NULL)", name=op.f('ck_legs_side_iff_team_market')),
    sa.CheckConstraint("(market_type LIKE 'player_%') = (espn_athlete_id IS NOT NULL)", name=op.f('ck_legs_athlete_iff_player_market')),
    sa.CheckConstraint("(result = 'pending') = (settlement_source IS NULL)", name=op.f('ck_legs_settled_has_source')),
    sa.CheckConstraint("market_type <> 'other' OR description IS NOT NULL", name=op.f('ck_legs_other_has_description')),
    sa.CheckConstraint("market_type = 'other' OR line IS NOT NULL", name=op.f('ck_legs_line_required')),
    sa.CheckConstraint('(verified_at IS NULL) = (verified_source IS NULL)', name=op.f('ck_legs_verified_pair')),
    sa.CheckConstraint('american_odds IS NULL OR american_odds >= 100 OR american_odds <= -100', name=op.f('ck_legs_odds_valid')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_legs_event_id_events')),
    sa.ForeignKeyConstraint(['slip_id'], ['slips.id'], name=op.f('fk_legs_slip_id_slips'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_legs'))
    )
    op.create_index(op.f('ix_legs_event_id'), 'legs', ['event_id'], unique=False)
    op.create_index(op.f('ix_legs_slip_id'), 'legs', ['slip_id'], unique=False)
    op.create_table('leg_tags',
    sa.Column('leg_id', sa.Integer(), nullable=False),
    sa.Column('tag_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['leg_id'], ['legs.id'], name=op.f('fk_leg_tags_leg_id_legs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_leg_tags_tag_id_tags'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('leg_id', 'tag_id', name=op.f('pk_leg_tags'))
    )


def downgrade() -> None:
    op.drop_table('leg_tags')
    op.drop_index(op.f('ix_legs_slip_id'), table_name='legs')
    op.drop_index(op.f('ix_legs_event_id'), table_name='legs')
    op.drop_table('legs')
    op.drop_table('slips')
    op.drop_table('tags')
    op.drop_table('sportsbooks')
    op.drop_table('source_health')
    op.drop_index('ix_raw_samples_source_fetched_at', table_name='raw_samples')
    op.drop_table('raw_samples')
    op.drop_table('events')

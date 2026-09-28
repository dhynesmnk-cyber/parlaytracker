"""Add tags.retired, so a tag can be hidden from pickers but kept on old legs (SPEC.md 9.7).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tags', sa.Column('retired', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('tags', 'retired')

"""add discount columns to landing_pages

Supports time-bounded discounts with per-tariff overrides and localized badge text.

Revision ID: 0027
Revises: 0026
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0027'
down_revision: Union[str, None] = '0026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if _has_column('landing_pages', 'discount_percent'):
        return

    op.add_column('landing_pages', sa.Column('discount_percent', sa.Integer(), nullable=True))
    op.add_column('landing_pages', sa.Column('discount_overrides', sa.JSON(), nullable=True))
    op.add_column(
        'landing_pages',
        sa.Column('discount_starts_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'landing_pages',
        sa.Column('discount_ends_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('landing_pages', sa.Column('discount_badge_text', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('landing_pages', 'discount_badge_text')
    op.drop_column('landing_pages', 'discount_ends_at')
    op.drop_column('landing_pages', 'discount_starts_at')
    op.drop_column('landing_pages', 'discount_overrides')
    op.drop_column('landing_pages', 'discount_percent')

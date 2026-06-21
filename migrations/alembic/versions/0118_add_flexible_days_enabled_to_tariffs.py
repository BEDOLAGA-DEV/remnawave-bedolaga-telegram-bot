"""add flexible_days_enabled to tariffs

Adds a per-tariff flag enabling arbitrary-day purchase priced from period_prices
anchors (floor-anchor with cap). Separate from the flat custom_days mechanism.

Revision ID: 0118
Revises: 0117
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0118'
down_revision: Union[str, None] = '0117'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c['name'] for c in inspector.get_columns('tariffs')}

    if 'flexible_days_enabled' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('flexible_days_enabled', sa.Boolean(), nullable=False, server_default='false'),
        )

    op.execute('UPDATE tariffs SET flexible_days_enabled = false WHERE flexible_days_enabled IS NULL')


def downgrade() -> None:
    op.drop_column('tariffs', 'flexible_days_enabled')

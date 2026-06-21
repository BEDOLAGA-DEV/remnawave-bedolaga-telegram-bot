"""add device_price_tiers to tariffs

Adds per-tariff non-linear device pricing:
  - device_price_tiers: JSON {"3": 4000, "5": 7000} (total_device_count: extra_kopeks_per_month
    over base). Empty = use linear device_price_kopeks (legacy behaviour).

Revision ID: 0117
Revises: 0116
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0117'
down_revision: Union[str, None] = '0116'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip column if it already exists from a custom branch.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c['name'] for c in inspector.get_columns('tariffs')}

    if 'device_price_tiers' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('device_price_tiers', sa.JSON(), nullable=True, server_default='{}'),
        )

    op.execute("UPDATE tariffs SET device_price_tiers = '{}' WHERE device_price_tiers IS NULL")


def downgrade() -> None:
    op.drop_column('tariffs', 'device_price_tiers')

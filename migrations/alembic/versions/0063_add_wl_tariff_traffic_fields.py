"""add wl traffic fields to tariffs

Adds per-tariff WL (БС) traffic configuration:
  - wl_default_traffic_gb: starting WL traffic limit for this tariff (NULL = use global WL_DEFAULT_TRAFFIC_LIMIT_GB)
  - wl_traffic_topup_packages: JSON {"5": 1000, "10": 2000, ...} (gb: price_kopeks). Empty = use global prices.

Revision ID: 0063
Revises: 0062
Create Date: 2026-03-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0063'
down_revision: Union[str, None] = '0062'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip columns that already exist from a custom branch.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c['name'] for c in inspector.get_columns('tariffs')}

    if 'wl_default_traffic_gb' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('wl_default_traffic_gb', sa.Integer(), nullable=True, server_default=None),
        )
    if 'wl_traffic_topup_packages' not in existing:
        op.add_column(
            'tariffs',
            sa.Column('wl_traffic_topup_packages', sa.JSON(), nullable=True, server_default='{}'),
        )

    # Initialize existing rows: NULL means "use global default"
    op.execute("UPDATE tariffs SET wl_traffic_topup_packages = '{}' WHERE wl_traffic_topup_packages IS NULL")


def downgrade() -> None:
    op.drop_column('tariffs', 'wl_traffic_topup_packages')
    op.drop_column('tariffs', 'wl_default_traffic_gb')

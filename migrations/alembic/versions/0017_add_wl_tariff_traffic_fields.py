"""add wl traffic fields to tariffs

Adds per-tariff WL (БС) traffic configuration:
  - wl_default_traffic_gb: starting WL traffic limit for this tariff (NULL = use global WL_DEFAULT_TRAFFIC_LIMIT_GB)
  - wl_traffic_topup_packages: JSON {"5": 1000, "10": 2000, ...} (gb: price_kopeks). Empty = use global prices.

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add WL traffic config columns to tariffs table
    op.add_column(
        'tariffs',
        sa.Column('wl_default_traffic_gb', sa.Integer(), nullable=True, server_default=None),
    )
    op.add_column(
        'tariffs',
        sa.Column('wl_traffic_topup_packages', sa.JSON(), nullable=True, server_default='{}'),
    )

    # Initialize existing rows: NULL means "use global default"
    op.execute("UPDATE tariffs SET wl_traffic_topup_packages = '{}' WHERE wl_traffic_topup_packages IS NULL")


def downgrade() -> None:
    op.drop_column('tariffs', 'wl_traffic_topup_packages')
    op.drop_column('tariffs', 'wl_default_traffic_gb')

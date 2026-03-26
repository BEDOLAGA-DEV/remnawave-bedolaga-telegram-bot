"""add trial_duration_days to tariffs

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-05

Adds per-tariff trial duration override column.
NULL means use global TRIAL_DURATION_DAYS setting.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = [c['name'] for c in sa.inspect(conn).get_columns('tariffs')]
    if 'trial_duration_days' not in columns:
        op.add_column('tariffs', sa.Column('trial_duration_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tariffs', 'trial_duration_days')

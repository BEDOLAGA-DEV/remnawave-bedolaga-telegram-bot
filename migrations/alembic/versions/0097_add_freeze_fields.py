"""add freeze fields to subscriptions

Revision ID: 0097
Revises: 0096
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('frozen_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('freeze_days_used_year', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('subscriptions', sa.Column('freeze_year', sa.Integer(), nullable=True))
    op.add_column('subscriptions', sa.Column('last_freeze_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'last_freeze_at')
    op.drop_column('subscriptions', 'freeze_year')
    op.drop_column('subscriptions', 'freeze_days_used_year')
    op.drop_column('subscriptions', 'frozen_until')
    op.drop_column('subscriptions', 'frozen_at')

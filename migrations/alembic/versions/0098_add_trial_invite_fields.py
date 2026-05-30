"""add trial-invite counters to users

Revision ID: 0098
Revises: 0097
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0098'
down_revision: Union[str, None] = '0097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('trial_invite_bonus_days_used', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('trial_invite_rewarded_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('users', 'trial_invite_rewarded_count')
    op.drop_column('users', 'trial_invite_bonus_days_used')

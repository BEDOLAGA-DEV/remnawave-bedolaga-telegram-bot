"""add partner withdrawal limits

Revision ID: 0120
Revises: 0119
Create Date: 2026-07-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0120'
down_revision: Union[str, None] = '0119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('referral_withdrawal_min_kopeks', sa.Integer(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('referral_withdrawal_cooldown_days', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'referral_withdrawal_cooldown_days')
    op.drop_column('users', 'referral_withdrawal_min_kopeks')

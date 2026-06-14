"""add transaction refund flag

Lets an admin mark a payment transaction as refunded so the returned money is
excluded from all revenue/spend statistics. Balance, subscription and referral
state are left untouched. Defaults to not-refunded for all existing rows.

Revision ID: 0116
Revises: 0115
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0116'
down_revision: Union[str, None] = '0115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'transactions',
        sa.Column('is_refunded', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column('transactions', sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('transactions', sa.Column('refunded_by', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('transactions', 'refunded_by')
    op.drop_column('transactions', 'refunded_at')
    op.drop_column('transactions', 'is_refunded')

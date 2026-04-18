"""create aurapay_payments table

Revision ID: 0060
Revises: 0059
Create Date: 2026-04-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0060'
down_revision: Union[str, None] = '0059'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'aurapay_payments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('order_id', sa.String(64), unique=True, nullable=False, index=True),
        sa.Column('aurapay_invoice_id', sa.String(128), unique=True, nullable=True, index=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('is_paid', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('payment_method', sa.String(32), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            'transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_table('aurapay_payments')

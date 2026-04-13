"""add external_gateway_payments table

Table for storing payments through external payment gateway (paygate).
Supports any HTTP-based payment provider with standard create/callback/status API.

Revision ID: 0039
Revises: 0038
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: Union[str, None] = '0038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'external_gateway_payments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
        sa.Column('order_id', sa.String(128), nullable=False, unique=True, index=True),
        sa.Column('gateway_order_id', sa.String(128), nullable=True, index=True),
        sa.Column('gateway_payment_id', sa.String(255), nullable=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
        sa.Column('amount_converted', sa.Numeric(12, 2), nullable=True),
        sa.Column('payment_method_name', sa.String(64), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('is_paid', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('external_gateway_payments')

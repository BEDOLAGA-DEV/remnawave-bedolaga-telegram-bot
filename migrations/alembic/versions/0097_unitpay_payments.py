"""unitpay_payments table

Revision ID: 0097
Revises: 0096
Create Date: 2026-06-30
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'unitpay_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('unitpay_id', sa.String(length=128), nullable=True),
        sa.Column('subscription_id', sa.String(length=128), nullable=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('is_paid', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('payment_type', sa.String(length=32), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_unitpay_payments_id', 'unitpay_payments', ['id'])
    op.create_index('ix_unitpay_payments_order_id', 'unitpay_payments', ['order_id'], unique=True)
    op.create_index('ix_unitpay_payments_unitpay_id', 'unitpay_payments', ['unitpay_id'], unique=True)
    op.create_index('ix_unitpay_payments_subscription_id', 'unitpay_payments', ['subscription_id'])
    op.create_index('ix_unitpay_payments_user_id', 'unitpay_payments', ['user_id'])


def downgrade() -> None:
    op.drop_table('unitpay_payments')

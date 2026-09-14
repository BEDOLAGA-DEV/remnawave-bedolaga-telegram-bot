"""create tabpay_payments table

Revision ID: 0113
Revises: 0112
Create Date: 2026-08-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0113'
down_revision: Union[str, None] = '0112'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tabpay_payments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('order_id', sa.String(64), unique=True, nullable=False, index=True),
        sa.Column('tabpay_payment_id', sa.String(64), unique=True, nullable=True, index=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('commission_kopeks', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('is_paid', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('is_test', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('payment_method', sa.String(32), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('processed_events', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
    )
    # Модель объявляет id как index=True, поэтому create_all на свежей установке
    # заводит этот индекс. Без него обновлённая база отличалась бы от новой, и
    # autogenerate вечно показывал бы фантомную разницу.
    op.create_index('ix_tabpay_payments_id', 'tabpay_payments', ['id'])


def downgrade() -> None:
    op.drop_table('tabpay_payments')

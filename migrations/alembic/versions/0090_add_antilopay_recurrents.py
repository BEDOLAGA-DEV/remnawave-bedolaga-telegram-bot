"""add antilopay_recurrents table and recurrent_id on antilopay_payments

Revision ID: 0090
Revises: 0089
Create Date: 2026-05-23

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0090'
down_revision: Union[str, None] = '0089'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('antilopay_payments', sa.Column('recurrent_id', sa.String(255), nullable=True))
    op.create_index('ix_antilopay_payments_recurrent_id', 'antilopay_payments', ['recurrent_id'])

    op.create_table(
        'antilopay_recurrents',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('recurrent_id', sa.String(255), unique=True, nullable=False),
        sa.Column('initial_payment_id', sa.String(128), nullable=True),
        sa.Column('recurrent_type', sa.String(10), nullable=False, server_default='MONTH'),
        sa.Column('payment_count', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(32), nullable=True),
        sa.Column('pay_method', sa.String(50), nullable=True),
        sa.Column('pay_data', sa.String(255), nullable=True),
        sa.Column('title', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_antilopay_recurrents_user_id', 'antilopay_recurrents', ['user_id'])
    op.create_index('ix_antilopay_recurrents_recurrent_id', 'antilopay_recurrents', ['recurrent_id'])
    op.create_index('ix_antilopay_recurrents_user_active', 'antilopay_recurrents', ['user_id', 'is_active'])


def downgrade() -> None:
    op.drop_table('antilopay_recurrents')
    op.drop_index('ix_antilopay_payments_recurrent_id', table_name='antilopay_payments')
    op.drop_column('antilopay_payments', 'recurrent_id')

"""add antilopay_recurrents table and recurrent_id on antilopay_payments

Revision ID: 0094
Revises: 0093
Create Date: 2026-06-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Check if column 'recurrent_id' in 'antilopay_payments' exists
    if inspector.has_table('antilopay_payments'):
        columns = [col['name'] for col in inspector.get_columns('antilopay_payments')]
        if 'recurrent_id' not in columns:
            op.add_column('antilopay_payments', sa.Column('recurrent_id', sa.String(255), nullable=True))
            op.create_index('ix_antilopay_payments_recurrent_id', 'antilopay_payments', ['recurrent_id'])

    # 2. Check if table 'antilopay_recurrents' exists
    if not inspector.has_table('antilopay_recurrents'):
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
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('antilopay_recurrents'):
        op.drop_table('antilopay_recurrents')

    if inspector.has_table('antilopay_payments'):
        columns = [col['name'] for col in inspector.get_columns('antilopay_payments')]
        if 'recurrent_id' in columns:
            op.drop_index('ix_antilopay_payments_recurrent_id', table_name='antilopay_payments')
            op.drop_column('antilopay_payments', 'recurrent_id')

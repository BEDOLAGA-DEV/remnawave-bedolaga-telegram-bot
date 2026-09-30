"""add cashera_subscriptions

Автопродление через подписки Cashera (sbp_recurring). Partial unique index на
живую привязку создаётся сразу — дублей на новой таблице нет.

Revision ID: 0131
Revises: 0130
"""

import sqlalchemy as sa
from alembic import op


revision = '0131'
down_revision = '0130'
branch_labels = None
depends_on = None

_ALIVE = "('PENDING', 'ACTIVE', 'PAST_DUE')"


def upgrade() -> None:
    op.create_table(
        'cashera_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id'), nullable=True),
        sa.Column('cashera_subscription_uuid', sa.String(length=64), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=False),
        sa.Column('interval', sa.String(length=16), nullable=False),
        sa.Column('charge_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('remote_status', sa.String(length=32), nullable=True),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_external_id', sa.String(length=255), nullable=True),
        sa.Column('charges_success', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('charges_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_cashera_subscriptions_user_id', 'cashera_subscriptions', ['user_id'])
    op.create_index('ix_cashera_subscriptions_subscription_id', 'cashera_subscriptions', ['subscription_id'])
    op.create_unique_constraint(
        'uq_cashera_subscriptions_uuid', 'cashera_subscriptions', ['cashera_subscription_uuid']
    )
    op.create_unique_constraint('uq_cashera_subscriptions_external_id', 'cashera_subscriptions', ['external_id'])
    op.create_index('ix_cashera_subscriptions_user_active', 'cashera_subscriptions', ['user_id', 'status'])
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_cashera_subscriptions_alive
            ON cashera_subscriptions (subscription_id)
            WHERE status IN {_ALIVE}
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text('DROP INDEX IF EXISTS uq_cashera_subscriptions_alive'))
    op.drop_table('cashera_subscriptions')

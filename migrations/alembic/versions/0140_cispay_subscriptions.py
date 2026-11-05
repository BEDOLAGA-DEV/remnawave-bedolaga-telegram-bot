"""Persist CisPay agreements, charge ledger and early events.

Revision ID: 0140
Revises: 0139
"""

import sqlalchemy as sa
from alembic import op


revision = '0140'
down_revision = '0139'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cispay_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL')),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('shop_id', sa.String(64), nullable=False),
        sa.Column('checkout_id', sa.String(36), nullable=False),
        sa.Column('provider_id', sa.String(36)),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('purpose', sa.String(255), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('remote_status', sa.String(20)),
        sa.Column('redirect_url', sa.Text()),
        sa.Column('reason', sa.String(64)),
        sa.Column('next_charge_at', sa.DateTime(timezone=True)),
        sa.Column('last_charge_at', sa.DateTime(timezone=True)),
        sa.Column('expected_end_date', sa.DateTime(timezone=True)),
        sa.Column('request_started_at', sa.DateTime(timezone=True)),
        sa.Column('retry_at', sa.DateTime(timezone=True)),
        sa.Column('last_reconciled_at', sa.DateTime(timezone=True)),
        sa.Column('reconcile_offset', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('recovery_candidates', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('shop_id', 'checkout_id', name='uq_cispay_subscription_checkout'),
        sa.UniqueConstraint('shop_id', 'provider_id', name='uq_cispay_subscription_remote'),
    )
    for column in ('user_id', 'subscription_id'):
        op.create_index(f'ix_cispay_subscriptions_{column}', 'cispay_subscriptions', [column])
    condition = sa.text("status IN ('CREATING','CREATION_UNKNOWN','PENDING','ACTIVE','PAUSED','CANCEL_PENDING')")
    op.create_index(
        'uq_cispay_subscription_live_target',
        'cispay_subscriptions',
        ['subscription_id'],
        unique=True,
        postgresql_where=condition,
        sqlite_where=condition,
    )
    condition = sa.text("status IN ('CREATING','CREATION_UNKNOWN')")
    op.create_index(
        'uq_cispay_subscription_unresolved_owner',
        'cispay_subscriptions',
        ['shop_id', 'owner_id'],
        unique=True,
        postgresql_where=condition,
        sqlite_where=condition,
    )
    op.create_table(
        'cispay_subscription_charges',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('agreement_id', sa.Integer(), sa.ForeignKey('cispay_subscriptions.id'), nullable=False),
        sa.Column('shop_id', sa.String(64), nullable=False),
        sa.Column('provider_transaction_id', sa.String(36), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
        sa.Column('paid_at', sa.DateTime(timezone=True)),
        sa.Column('delivery_completed_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('shop_id', 'provider_transaction_id', name='uq_cispay_subscription_charge'),
    )
    op.create_index('ix_cispay_subscription_charges_agreement_id', 'cispay_subscription_charges', ['agreement_id'])
    op.create_table(
        'cispay_subscription_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('shop_id', sa.String(64), nullable=False),
        sa.Column('event_key', sa.String(100), nullable=False),
        sa.Column('provider_subscription_id', sa.String(36), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('shop_id', 'event_key', name='uq_cispay_subscription_event'),
    )
    op.create_index(
        'ix_cispay_subscription_events_provider_subscription_id',
        'cispay_subscription_events',
        ['provider_subscription_id'],
    )


def downgrade():
    op.drop_table('cispay_subscription_events')
    op.drop_table('cispay_subscription_charges')
    op.drop_table('cispay_subscriptions')

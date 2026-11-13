"""Add PayHot consent and charge journal, without enabling recurring runtime.

Revision ID: 0143
Revises: 0142
"""

import sqlalchemy as sa
from alembic import op

revision = '0143'
down_revision = '0142'
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table('payhot_agreements'):
        op.create_table(
            'payhot_agreements',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
            sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL')),
            sa.Column('owner_id', sa.Integer(), nullable=False),
            sa.Column('checkout_id', sa.String(128), nullable=False),
            sa.Column('mandate_id', sa.String(128), unique=True),
            sa.Column('first_payment_id', sa.Integer(), sa.ForeignKey('payhot_payments.id', ondelete='SET NULL')),
            sa.Column('consent_snapshot', sa.JSON(), nullable=False),
            sa.Column('status', sa.String(32), nullable=False),
            sa.Column('cancel_requested_at', sa.DateTime(timezone=True)),
            sa.Column('next_charge_at', sa.DateTime(timezone=True)),
            sa.Column('reason', sa.String(64)),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('owner_id', 'checkout_id', name='uq_payhot_agreement_checkout'),
        )
        for column in ('user_id', 'subscription_id'):
            op.create_index(f'ix_payhot_agreements_{column}', 'payhot_agreements', [column])
        op.create_index(
            'uq_payhot_agreement_live_target',
            'payhot_agreements',
            ['subscription_id'],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('CREATING','CREATION_UNKNOWN','PENDING','ACTIVE','PAUSED','CANCEL_PENDING')"
            ),
        )
    if not sa.inspect(op.get_bind()).has_table('payhot_charge_attempts'):
        op.create_table(
            'payhot_charge_attempts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'agreement_id', sa.Integer(), sa.ForeignKey('payhot_agreements.id', ondelete='RESTRICT'), nullable=False
            ),
            sa.Column('cycle_key', sa.String(128), nullable=False),
            sa.Column('provider_charge_id', sa.String(128), unique=True),
            sa.Column('idempotency_key', sa.String(128), nullable=False, unique=True),
            sa.Column('merchant_reference', sa.String(128), nullable=False, unique=True),
            sa.Column('request_payload', sa.JSON(), nullable=False),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(10), nullable=False),
            sa.Column('expected_end_date', sa.DateTime(timezone=True), nullable=False),
            sa.Column('status', sa.String(32), nullable=False),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
            sa.Column('accepted_at', sa.DateTime(timezone=True)),
            sa.Column('delivery_completed_at', sa.DateTime(timezone=True)),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('agreement_id', 'cycle_key', name='uq_payhot_charge_cycle'),
            sa.UniqueConstraint('agreement_id', 'expected_end_date', name='uq_payhot_charge_boundary'),
            sa.CheckConstraint("amount_kopeks > 0 AND currency = 'RUB'", name='ck_payhot_charge_rub_principal'),
        )
        op.create_index('ix_payhot_charge_attempts_agreement_id', 'payhot_charge_attempts', ['agreement_id'])

        op.create_index(
            'uq_payhot_charge_unresolved',
            'payhot_charge_attempts',
            ['agreement_id'],
            unique=True,
            postgresql_where=sa.text(
                "status IN ('RESERVED','UNKNOWN','PROCESSING','REVIEW','REFUND_REVIEW') OR (status = 'SUCCEEDED' AND delivery_completed_at IS NULL)"
            ),
        )


def downgrade():
    for table in ('payhot_charge_attempts', 'payhot_agreements'):
        if sa.inspect(op.get_bind()).has_table(table):
            if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
                raise RuntimeError('Cannot remove populated PayHot financial journal')
    for table in ('payhot_charge_attempts', 'payhot_agreements'):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)

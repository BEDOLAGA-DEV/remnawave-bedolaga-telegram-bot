"""Persist tariff device checkout identity alongside the wallet ledger.

Revision ID: 0137
Revises: 0136
"""

import sqlalchemy as sa
from alembic import op


revision = '0137'
down_revision = '0136'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('tariff_purchase_intents'):
        return
    op.create_table(
        'tariff_purchase_intents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('checkout_id', sa.String(36), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL')),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
        sa.Column('charged_kopeks', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'checkout_id', name='uq_tariff_purchase_checkout'),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('tariff_purchase_intents'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM tariff_purchase_intents LIMIT 1')).first() is not None:
            raise RuntimeError('Cannot discard completed tariff checkout identities')
        op.drop_table('tariff_purchase_intents')

"""add platega_subscriptions

Revision ID: 0099
Revises: 0098
"""

from alembic import op
import sqlalchemy as sa

revision = '0099'
down_revision = '0098'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'platega_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id'), nullable=True),
        sa.Column('platega_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('interval', sa.Integer(), nullable=False),
        sa.Column('charge_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_external_id', sa.String(length=255), nullable=True),
        sa.Column('charges_success', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('charges_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_platega_subscriptions_user_id', 'platega_subscriptions', ['user_id'])
    op.create_index('ix_platega_subscriptions_subscription_id', 'platega_subscriptions', ['subscription_id'])
    op.create_unique_constraint(
        'uq_platega_subscriptions_platega_id', 'platega_subscriptions', ['platega_subscription_id']
    )
    op.create_index('ix_platega_subscriptions_user_active', 'platega_subscriptions', ['user_id', 'status'])


def downgrade() -> None:
    op.drop_table('platega_subscriptions')

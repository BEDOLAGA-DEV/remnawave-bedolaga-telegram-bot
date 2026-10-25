"""Persist daily paid traffic reset delivery and ambiguous acknowledgements.

Revision ID: 0134
Revises: 0133
"""

import sqlalchemy as sa
from alembic import op

revision = '0134'
down_revision = '0133'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('paid_traffic_reset_intents'):
        return
    op.create_table(
        'paid_traffic_reset_intents',
        sa.CheckConstraint(
            "state IN ('pending', 'attempted', 'confirmed', 'blocked', 'superseded')", name='ck_paid_reset_state'
        ),
        sa.CheckConstraint(
            "state != 'attempted' OR (target_panel_id IS NOT NULL AND target_short_uuid IS NOT NULL AND attempted_at IS NOT NULL)",
            name='ck_paid_reset_attempt_target',
        ),
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL'), nullable=True
        ),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column(
            'transaction_id',
            sa.Integer(),
            sa.ForeignKey('transactions.id', ondelete='SET NULL'),
            nullable=True,
            unique=True,
        ),
        sa.Column('operation_key', sa.String(128), nullable=False, unique=True),
        sa.Column('charge_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(128), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('target_panel_id', sa.BigInteger(), nullable=True),
        sa.Column('target_short_uuid', sa.String(255), nullable=True),
        sa.Column('baseline_observed', sa.Boolean(), nullable=False),
        sa.Column('baseline_last_reset_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('activation_pending', sa.Boolean(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('confirmed_last_reset_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('checks', sa.Integer(), nullable=False),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_paid_traffic_reset_due', 'paid_traffic_reset_intents', ['state', 'next_check_at', 'id'])
    op.create_index('ix_paid_traffic_reset_subscription', 'paid_traffic_reset_intents', ['subscription_id', 'id'])

    op.create_index(
        'uq_paid_traffic_reset_unresolved_target',
        'paid_traffic_reset_intents',
        ['target_panel_id'],
        unique=True,
        postgresql_where=sa.text("attempted_at IS NOT NULL AND state IN ('attempted', 'blocked')"),
        sqlite_where=sa.text("attempted_at IS NOT NULL AND state IN ('attempted', 'blocked')"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table('paid_traffic_reset_intents'):
        unresolved = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM paid_traffic_reset_intents WHERE state IN ('pending', 'attempted') "
                    "OR (state = 'blocked' AND attempted_at IS NOT NULL) OR activation_pending LIMIT 1"
                )
            )
            .first()
        )
        if unresolved is not None:
            raise RuntimeError('Resolve paid traffic reset intents before downgrading 0134')
        op.drop_table('paid_traffic_reset_intents')

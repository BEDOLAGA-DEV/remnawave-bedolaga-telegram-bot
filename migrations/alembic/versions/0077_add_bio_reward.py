"""add bio reward tables and subscription column

Bio-reward feature: free subscription + discount for users who add a marketing
string (or their personal referral link) to their Telegram bio.

Revision ID: 0077
Revises: 0076
Create Date: 2026-05-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0077'
down_revision: str | None = '0076'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('bio_reward_discount_percent', sa.Integer(), nullable=True),
    )

    op.create_table(
        'bio_reward_config',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='20'),
        sa.Column('grace_period_hours', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('cooldown_hours', sa.Integer(), nullable=False, server_default='48'),
        sa.Column('check_interval_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('free_sub_window_days', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('free_sub_traffic_gb_per_day', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('free_sub_device_limit', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('free_sub_squad_uuid', sa.String(255), nullable=True),
        sa.Column('accepted_bio_strings', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('match_personal_referral_link', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('notify_on_opt_in', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('notify_on_activate', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('notify_on_grace', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('notify_on_revoke', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('instruction_text', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'bio_reward_participants',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('opted_in_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_bio_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('grace_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cooldown_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'free_subscription_id',
            sa.Integer(),
            sa.ForeignKey('subscriptions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('bio_snapshot', sa.Text(), nullable=True),
        sa.Column('bypass_check', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', name='uq_bio_reward_participants_user_id'),
    )
    op.create_index('ix_bio_reward_participants_user_id', 'bio_reward_participants', ['user_id'])
    op.create_index('ix_bio_reward_participants_status', 'bio_reward_participants', ['status'])
    op.create_index(
        'ix_bio_reward_participants_cooldown_until',
        'bio_reward_participants',
        ['cooldown_until'],
    )

    op.create_table(
        'bio_reward_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'participant_id',
            sa.Integer(),
            sa.ForeignKey('bio_reward_participants.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_bio_reward_events_participant_id', 'bio_reward_events', ['participant_id'])
    op.create_index('ix_bio_reward_events_created_at', 'bio_reward_events', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_bio_reward_events_created_at', table_name='bio_reward_events')
    op.drop_index('ix_bio_reward_events_participant_id', table_name='bio_reward_events')
    op.drop_table('bio_reward_events')

    op.drop_index('ix_bio_reward_participants_cooldown_until', table_name='bio_reward_participants')
    op.drop_index('ix_bio_reward_participants_status', table_name='bio_reward_participants')
    op.drop_index('ix_bio_reward_participants_user_id', table_name='bio_reward_participants')
    op.drop_table('bio_reward_participants')

    op.drop_table('bio_reward_config')

    op.drop_column('subscriptions', 'bio_reward_discount_percent')

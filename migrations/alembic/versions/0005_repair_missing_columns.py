"""repair missing columns from skipped migrations

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-23

Some databases had auto-stamp to 'head' applied before migrations 0002-0004
were actually executed, leaving the alembic_version at 0004 but missing
columns/tables that those migrations would have created. This migration
re-checks and applies any missing schema changes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def _has_table(table: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table in inspector.get_table_names()


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def _has_constraint(table: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return constraint_name in [fk['name'] for fk in inspector.get_foreign_keys(table)]


def upgrade() -> None:
    # --- From 0002: referral_earnings.campaign_id ---
    if _has_table('referral_earnings') and not _has_column('referral_earnings', 'campaign_id'):
        op.add_column('referral_earnings', sa.Column('campaign_id', sa.Integer(), nullable=True))

        if _has_table('advertising_campaigns'):
            op.create_foreign_key(
                'fk_referral_earnings_campaign_id',
                'referral_earnings',
                'advertising_campaigns',
                ['campaign_id'],
                ['id'],
                ondelete='SET NULL',
            )

        op.create_index('ix_referral_earnings_campaign_id', 'referral_earnings', ['campaign_id'])

        # Backfill from advertising_campaign_registrations
        if _has_table('advertising_campaign_registrations'):
            op.execute(
                sa.text("""
                UPDATE referral_earnings re
                SET campaign_id = sub.campaign_id
                FROM (
                    SELECT DISTINCT ON (user_id) user_id, campaign_id
                    FROM advertising_campaign_registrations
                    ORDER BY user_id, created_at ASC
                ) sub
                WHERE sub.user_id = re.referral_id
                  AND re.campaign_id IS NULL
                """)
            )

    # --- From 0003: users.partner_status ---
    if not _has_column('users', 'partner_status'):
        op.add_column('users', sa.Column('partner_status', sa.String(20), nullable=False, server_default='none'))
        op.create_index('ix_users_partner_status', 'users', ['partner_status'])

    # --- From 0003: broadcast_history.blocked_count ---
    if _has_table('broadcast_history') and not _has_column('broadcast_history', 'blocked_count'):
        op.add_column('broadcast_history', sa.Column('blocked_count', sa.Integer(), nullable=True, server_default='0'))

    # --- From 0003: advertising_campaigns.partner_user_id ---
    if _has_table('advertising_campaigns') and not _has_column('advertising_campaigns', 'partner_user_id'):
        op.add_column('advertising_campaigns', sa.Column('partner_user_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_advertising_campaigns_partner_user_id',
            'advertising_campaigns',
            'users',
            ['partner_user_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_index('ix_advertising_campaigns_partner_user_id', 'advertising_campaigns', ['partner_user_id'])

    # --- From 0003: withdrawal_requests ---
    if not _has_table('withdrawal_requests'):
        op.create_table(
            'withdrawal_requests',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(50), nullable=False, server_default='pending', index=True),
            sa.Column('payment_details', sa.Text(), nullable=True),
            sa.Column('risk_score', sa.Integer(), server_default='0'),
            sa.Column('risk_analysis', sa.Text(), nullable=True),
            sa.Column('processed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('admin_comment', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # --- From 0003: partner_applications ---
    if not _has_table('partner_applications'):
        op.create_table(
            'partner_applications',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('company_name', sa.String(255), nullable=True),
            sa.Column('website_url', sa.String(500), nullable=True),
            sa.Column('telegram_channel', sa.String(255), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('expected_monthly_referrals', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('admin_comment', sa.Text(), nullable=True),
            sa.Column('approved_commission_percent', sa.Integer(), nullable=True),
            sa.Column('processed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # --- From 0004: email_templates ---
    if not _has_table('email_templates'):
        op.create_table(
            'email_templates',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('notification_type', sa.String(100), nullable=False),
            sa.Column('language', sa.String(10), nullable=False),
            sa.Column('subject', sa.String(500), nullable=False),
            sa.Column('body_html', sa.Text(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('notification_type', 'language', name='uq_email_templates_type_lang'),
        )
        op.create_index('ix_email_templates_notification_type', 'email_templates', ['notification_type'])


def downgrade() -> None:
    # This is a repair migration — downgrade is a no-op.
    # The original migrations handle their own downgrades.
    pass

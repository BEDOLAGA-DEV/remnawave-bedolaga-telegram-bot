"""add partner system tables and columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-18

Adds partner_status to users, creates withdrawal_requests and
partner_applications tables, adds partner_user_id to advertising_campaigns,
adds blocked_count to broadcast_history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
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


def upgrade() -> None:
    # 1. users.partner_status
    if not _has_column('users', 'partner_status'):
        op.add_column('users', sa.Column('partner_status', sa.String(20), nullable=False, server_default='none'))
        op.create_index('ix_users_partner_status', 'users', ['partner_status'])

    # 2. broadcast_history.blocked_count
    if _has_table('broadcast_history') and not _has_column('broadcast_history', 'blocked_count'):
        op.add_column('broadcast_history', sa.Column('blocked_count', sa.Integer(), nullable=True, server_default='0'))

    # 3. advertising_campaigns.partner_user_id
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

    # 4. withdrawal_requests table
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

    # 5. partner_applications table
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


def downgrade() -> None:
    op.drop_table('partner_applications')
    op.drop_table('withdrawal_requests')
    op.drop_index('ix_advertising_campaigns_partner_user_id', table_name='advertising_campaigns')
    op.drop_constraint('fk_advertising_campaigns_partner_user_id', 'advertising_campaigns', type_='foreignkey')
    op.drop_column('advertising_campaigns', 'partner_user_id')
    op.drop_column('broadcast_history', 'blocked_count')
    op.drop_index('ix_users_partner_status', table_name='users')
    op.drop_column('users', 'partner_status')

"""add web_push_subscriptions table

Revision ID: 0064
Revises: 0063
Create Date: 2026-04-11

Adds Web Push (VAPID) subscription storage for browser push notifications.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0064'
down_revision: Union[str, None] = '0063'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'web_push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=255), nullable=False),
        sa.Column('auth', sa.String(length=255), nullable=False),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'endpoint', name='uq_webpush_user_endpoint'),
    )
    op.create_index('ix_web_push_subscriptions_id', 'web_push_subscriptions', ['id'])
    op.create_index('ix_webpush_user_active', 'web_push_subscriptions', ['user_id', 'is_active'])


def downgrade() -> None:
    op.drop_index('ix_webpush_user_active', table_name='web_push_subscriptions')
    op.drop_index('ix_web_push_subscriptions_id', table_name='web_push_subscriptions')
    op.drop_table('web_push_subscriptions')

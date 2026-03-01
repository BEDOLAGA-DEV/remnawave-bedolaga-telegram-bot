"""add required_channels and user_channel_subscriptions tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    """Check if table already exists (idempotency guard)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
            "WHERE table_schema = 'public' AND table_name = :name)"
        ),
        {'name': table_name},
    )
    return result.scalar()


def upgrade() -> None:
    if not _has_table('required_channels'):
        op.create_table(
            'required_channels',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('channel_id', sa.String(100), unique=True, nullable=False),
            sa.Column('channel_link', sa.String(500), nullable=True),
            sa.Column('title', sa.String(255), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table('user_channel_subscriptions'):
        op.create_table(
            'user_channel_subscriptions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('telegram_id', sa.BigInteger(), nullable=False),
            sa.Column('channel_id', sa.String(100), nullable=False),
            sa.Column('is_member', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('telegram_id', 'channel_id', name='uq_user_channel_sub'),
            sa.Index('ix_user_channel_sub_telegram_id', 'telegram_id'),
        )


def downgrade() -> None:
    op.drop_table('user_channel_subscriptions')
    op.drop_table('required_channels')

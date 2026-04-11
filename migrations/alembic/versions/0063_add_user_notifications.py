"""add user_notifications table

Revision ID: 0063
Revises: 0062
Create Date: 2026-04-11

Adds persistent in-app notification inbox for users.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0063'
down_revision: Union[str, None] = '0062'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('level', sa.String(length=20), server_default='info', nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('action_url', sa.String(length=500), nullable=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_notifications_user_id', 'user_notifications', ['user_id'])
    op.create_index('ix_user_notifications_user_created', 'user_notifications', ['user_id', 'created_at'])
    op.create_index('ix_user_notifications_user_unread', 'user_notifications', ['user_id', 'read_at'])


def downgrade() -> None:
    op.drop_index('ix_user_notifications_user_unread', table_name='user_notifications')
    op.drop_index('ix_user_notifications_user_created', table_name='user_notifications')
    op.drop_index('ix_user_notifications_user_id', table_name='user_notifications')
    op.drop_table('user_notifications')

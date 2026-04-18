"""add user_reviews table

Reviews for bonus with public channel forwarding.

Revision ID: 0067
Revises: 0066
Create Date: 2026-04-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0067'
down_revision: Union[str, None] = '0066'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'user_reviews' in inspector.get_table_names():
        return

    op.create_table(
        'user_reviews',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('rating', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('bonus_kopeks', sa.Integer(), server_default='0'),
        sa.Column('is_approved', sa.Boolean(), server_default='false'),
        sa.Column('channel_message_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', name='uq_user_review_user'),
    )
    op.create_index('ix_user_reviews_id', 'user_reviews', ['id'])


def downgrade() -> None:
    op.drop_index('ix_user_reviews_id', table_name='user_reviews')
    op.drop_table('user_reviews')

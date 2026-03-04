"""add ai_faq_media table

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-03

Таблица медиа-вложений для FAQ-статей.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return inspector.has_table(name)


def upgrade() -> None:
    if not _has_table('ai_faq_media'):
        op.create_table(
            'ai_faq_media',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('article_id', sa.Integer(), sa.ForeignKey('ai_faq_articles.id', ondelete='CASCADE'), nullable=False, index=True),
            sa.Column('media_type', sa.String(20), nullable=False),
            sa.Column('file_id', sa.String(512), nullable=False),
            sa.Column('caption', sa.String(1024), nullable=True),
            sa.Column('tag', sa.String(50), nullable=False, unique=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table('ai_faq_media')

"""add help_articles table

Help Center / FAQ articles for the cabinet.

Revision ID: 0076
Revises: 0075
Create Date: 2026-04-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0076'
down_revision: str | None = '0075'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'help_articles',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('slug', sa.String(500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False, server_default=''),
        sa.Column('excerpt', sa.Text(), nullable=True),
        sa.Column('category', sa.String(100), nullable=False, server_default='general'),
        sa.Column('category_icon', sa.String(32), nullable=True),
        sa.Column('category_color', sa.String(20), nullable=False, server_default='#00e5a0'),
        sa.Column('locale', sa.String(10), nullable=False, server_default='ru'),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('is_featured', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('views_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('helpful_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('not_helpful_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('locale', 'slug', name='uq_help_articles_locale_slug'),
    )

    op.create_index('ix_help_articles_slug', 'help_articles', ['slug'])
    op.create_index(
        'ix_help_articles_published_locale',
        'help_articles',
        ['is_published', 'locale'],
    )
    op.create_index(
        'ix_help_articles_published_category',
        'help_articles',
        ['is_published', 'category'],
    )
    op.create_index('ix_help_articles_created_at', 'help_articles', ['created_at'])
    op.create_index(
        'ix_help_articles_category_order',
        'help_articles',
        ['category', 'display_order'],
    )


def downgrade() -> None:
    op.drop_table('help_articles')

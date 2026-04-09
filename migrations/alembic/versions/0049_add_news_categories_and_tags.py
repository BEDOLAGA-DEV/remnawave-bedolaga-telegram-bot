"""add news_categories and news_tags tables with FK columns on news_articles

Revision ID: 0049
Revises: 0048
Create Date: 2026-03-23

Adds managed categories and tags for news articles.
- Creates news_categories table with case-insensitive unique name index
- Creates news_tags table with case-insensitive unique name index
- Adds category_id and tag_id FK columns to news_articles
- Backfills categories/tags from existing article data
- Populates FK columns via UPDATE ... FROM matching on lower(name)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0049'
down_revision: str | None = '0048'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- news_categories ---
    op.create_table(
        'news_categories',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('color', sa.String(20), nullable=False, server_default='#00e5a0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX ix_news_categories_name_lower ON news_categories (lower(name))"
        )
    )

    # --- news_tags ---
    op.create_table(
        'news_tags',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('color', sa.String(20), nullable=False, server_default='#94a3b8'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX ix_news_tags_name_lower ON news_tags (lower(name))"
        )
    )

    # --- FK columns on news_articles ---
    op.add_column('news_articles', sa.Column('category_id', sa.Integer(), nullable=True))
    op.add_column('news_articles', sa.Column('tag_id', sa.Integer(), nullable=True))

    op.create_foreign_key(
        'fk_news_articles_category_id',
        'news_articles',
        'news_categories',
        ['category_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_news_articles_tag_id',
        'news_articles',
        'news_tags',
        ['tag_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # --- Indexes on FK columns for efficient lookups and ON DELETE SET NULL ---
    op.create_index('ix_news_articles_category_id', 'news_articles', ['category_id'])
    op.create_index('ix_news_articles_tag_id', 'news_articles', ['tag_id'])

    # --- Backfill: seed categories from existing article data ---
    op.execute(
        sa.text(
            "INSERT INTO news_categories (name, color) "
            "SELECT DISTINCT category, category_color FROM news_articles "
            "WHERE category IS NOT NULL AND category != '' "
            "ON CONFLICT DO NOTHING"
        )
    )

    # --- Backfill: seed tags from existing article data ---
    op.execute(
        sa.text(
            "INSERT INTO news_tags (name) "
            "SELECT DISTINCT tag FROM news_articles "
            "WHERE tag IS NOT NULL AND tag != '' "
            "ON CONFLICT DO NOTHING"
        )
    )

    # --- Populate FK columns ---
    op.execute(
        sa.text(
            "UPDATE news_articles SET category_id = nc.id "
            "FROM news_categories nc "
            "WHERE lower(news_articles.category) = lower(nc.name) "
            "AND news_articles.category IS NOT NULL AND news_articles.category != ''"
        )
    )
    op.execute(
        sa.text(
            "UPDATE news_articles SET tag_id = nt.id "
            "FROM news_tags nt "
            "WHERE lower(news_articles.tag) = lower(nt.name) "
            "AND news_articles.tag IS NOT NULL AND news_articles.tag != ''"
        )
    )


def downgrade() -> None:
    op.execute(sa.text('DROP INDEX IF EXISTS ix_news_articles_tag_id'))
    op.execute(sa.text('DROP INDEX IF EXISTS ix_news_articles_category_id'))
    op.drop_constraint('fk_news_articles_tag_id', 'news_articles', type_='foreignkey')
    op.drop_constraint('fk_news_articles_category_id', 'news_articles', type_='foreignkey')
    op.drop_column('news_articles', 'tag_id')
    op.drop_column('news_articles', 'category_id')
    op.drop_table('news_tags')
    op.drop_table('news_categories')

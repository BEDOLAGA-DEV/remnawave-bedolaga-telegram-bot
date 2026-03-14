"""convert landing_pages text fields to JSON locale dicts

Revision ID: 0021
Revises: 0020
Create Date: 2026-03-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: Union[str, None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. Data migration: wrap existing plain-text values into {"ru": value} ---

    # title (NOT NULL) — always wrap
    op.execute("""
        UPDATE landing_pages
        SET title = jsonb_build_object('ru', title)
        WHERE title IS NOT NULL AND title != '';
    """)
    op.execute("""
        UPDATE landing_pages
        SET title = '{"ru": ""}'::jsonb
        WHERE title IS NULL OR title = '';
    """)

    # subtitle (nullable)
    op.execute("""
        UPDATE landing_pages
        SET subtitle = jsonb_build_object('ru', subtitle)
        WHERE subtitle IS NOT NULL;
    """)

    # footer_text (nullable)
    op.execute("""
        UPDATE landing_pages
        SET footer_text = jsonb_build_object('ru', footer_text)
        WHERE footer_text IS NOT NULL;
    """)

    # meta_title (nullable)
    op.execute("""
        UPDATE landing_pages
        SET meta_title = jsonb_build_object('ru', meta_title)
        WHERE meta_title IS NOT NULL;
    """)

    # meta_description (nullable)
    op.execute("""
        UPDATE landing_pages
        SET meta_description = jsonb_build_object('ru', meta_description)
        WHERE meta_description IS NOT NULL;
    """)

    # features — wrap title and description inside each array element
    op.execute("""
        UPDATE landing_pages
        SET features = (
            SELECT COALESCE(jsonb_agg(
                jsonb_set(
                    jsonb_set(
                        elem,
                        '{title}',
                        jsonb_build_object('ru', COALESCE(elem->>'title', ''))
                    ),
                    '{description}',
                    jsonb_build_object('ru', COALESCE(elem->>'description', ''))
                )
            ), '[]'::jsonb)
            FROM jsonb_array_elements(features::jsonb) AS elem
        )
        WHERE features IS NOT NULL
          AND features::text != '[]'
          AND features::text != 'null'
          AND jsonb_array_length(features::jsonb) > 0;
    """)

    # --- 2. ALTER COLUMN types: String/Text -> JSON ---
    # Must drop server_default before type change — PG can't auto-cast defaults
    op.alter_column('landing_pages', 'title', server_default=None)
    op.alter_column(
        'landing_pages',
        'title',
        type_=sa.JSON(),
        postgresql_using='title::jsonb',
        nullable=False,
    )
    op.execute("ALTER TABLE landing_pages ALTER COLUMN title SET DEFAULT '{}'::jsonb")

    op.alter_column(
        'landing_pages',
        'subtitle',
        type_=sa.JSON(),
        postgresql_using='subtitle::jsonb',
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'footer_text',
        type_=sa.JSON(),
        postgresql_using='footer_text::jsonb',
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'meta_title',
        type_=sa.JSON(),
        postgresql_using='meta_title::jsonb',
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'meta_description',
        type_=sa.JSON(),
        postgresql_using='meta_description::jsonb',
        nullable=True,
    )


def downgrade() -> None:
    # --- 1. Extract 'ru' key back into plain strings ---

    # First, convert JSON columns back to text type
    op.alter_column(
        'landing_pages',
        'title',
        type_=sa.String(500),
        postgresql_using="title->>'ru'",
        server_default='',
        nullable=False,
    )
    op.alter_column(
        'landing_pages',
        'subtitle',
        type_=sa.Text(),
        postgresql_using="subtitle->>'ru'",
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'footer_text',
        type_=sa.Text(),
        postgresql_using="footer_text->>'ru'",
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'meta_title',
        type_=sa.String(200),
        postgresql_using="meta_title->>'ru'",
        nullable=True,
    )
    op.alter_column(
        'landing_pages',
        'meta_description',
        type_=sa.Text(),
        postgresql_using="meta_description->>'ru'",
        nullable=True,
    )

    # Restore features array: extract 'ru' from nested title/description dicts
    op.execute("""
        UPDATE landing_pages
        SET features = (
            SELECT COALESCE(jsonb_agg(
                jsonb_set(
                    jsonb_set(
                        elem,
                        '{title}',
                        to_jsonb(COALESCE(elem->'title'->>'ru', ''))
                    ),
                    '{description}',
                    to_jsonb(COALESCE(elem->'description'->>'ru', ''))
                )
            ), '[]'::jsonb)
            FROM jsonb_array_elements(features::jsonb) AS elem
        )
        WHERE features IS NOT NULL
          AND features::text != '[]'
          AND features::text != 'null'
          AND jsonb_array_length(features::jsonb) > 0;
    """)

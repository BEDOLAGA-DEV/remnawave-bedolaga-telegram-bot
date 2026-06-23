"""add referrer_id to landing_pages

Revision ID: 0096
Revises: 0095
Create Date: 2026-06-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade() -> None:
    if not _column_exists('landing_pages', 'referrer_id'):
        op.add_column(
            'landing_pages',
            sa.Column(
                'referrer_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='SET NULL'),
                nullable=True,
            ),
        )
        op.create_index(
            'ix_landing_pages_referrer_id',
            'landing_pages',
            ['referrer_id'],
        )


def downgrade() -> None:
    if _column_exists('landing_pages', 'referrer_id'):
        op.drop_index('ix_landing_pages_referrer_id', table_name='landing_pages')
        op.drop_column('landing_pages', 'referrer_id')

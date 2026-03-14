"""Add background_config to landing_pages

Revision ID: 0030
Revises: 0029
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0030'
down_revision: Union[str, None] = '0029'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('landing_pages', 'background_config'):
        op.add_column('landing_pages', sa.Column('background_config', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('landing_pages', 'background_config')

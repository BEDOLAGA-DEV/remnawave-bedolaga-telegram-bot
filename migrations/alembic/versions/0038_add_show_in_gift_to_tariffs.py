"""add show_in_gift to tariffs

Boolean flag to control tariff visibility in the gift section.
Defaults to True so all existing tariffs remain visible.

Revision ID: 0038
Revises: 0037
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0038'
down_revision: Union[str, None] = '0037'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('tariffs', 'show_in_gift'):
        op.add_column(
            'tariffs',
            sa.Column('show_in_gift', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )


def downgrade() -> None:
    op.drop_column('tariffs', 'show_in_gift')

"""add page_type to info_pages

Revision ID: 0066
Revises: 0065
Create Date: 2026-04-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0066'
down_revision: Union[str, None] = '0065'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'info_pages',
        sa.Column('page_type', sa.String(20), nullable=False, server_default='page'),
    )


def downgrade() -> None:
    op.drop_column('info_pages', 'page_type')

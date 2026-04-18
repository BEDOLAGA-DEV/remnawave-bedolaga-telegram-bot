"""add broadcast category column

Revision ID: 0054
Revises: 0053
Create Date: 2026-04-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0054'
down_revision: Union[str, None] = '0053'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'broadcast_history',
        sa.Column('category', sa.String(20), nullable=False, server_default='system'),
    )


def downgrade() -> None:
    op.drop_column('broadcast_history', 'category')

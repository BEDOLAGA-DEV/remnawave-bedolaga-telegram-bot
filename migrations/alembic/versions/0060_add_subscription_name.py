"""add subscription name

Revision ID: 0060
Revises: 0059
Create Date: 2026-04-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0060'
down_revision: Union[str, None] = '0059'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('name', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'name')

"""add last_revoke_at to subscriptions

Revision ID: 0071
Revises: 0070
Create Date: 2026-05-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0071'
down_revision: Union[str, None] = '0070'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('last_revoke_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'last_revoke_at')

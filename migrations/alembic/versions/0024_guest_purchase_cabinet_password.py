"""add cabinet_password column to guest_purchases

Stores the generated plain-text password for guest email purchasers
so it can be shown on the success page and sent in credentials email.

Revision ID: 0024
Revises: 0023
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: Union[str, None] = '0023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('guest_purchases', 'cabinet_password'):
        op.add_column('guest_purchases', sa.Column('cabinet_password', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('guest_purchases', 'cabinet_password')

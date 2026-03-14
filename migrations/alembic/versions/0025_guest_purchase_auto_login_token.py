"""add auto_login_token column to guest_purchases

Stores the pre-generated auto-login JWT so it is not regenerated on every poll.

Revision ID: 0025
Revises: 0024
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0025'
down_revision: Union[str, None] = '0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('guest_purchases', 'auto_login_token'):
        op.add_column('guest_purchases', sa.Column('auto_login_token', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('guest_purchases', 'auto_login_token')

"""add index on guest_purchases.landing_id

Revision ID: 0020
Revises: 0019
Create Date: 2026-03-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: Union[str, None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_index('guest_purchases', 'ix_guest_purchases_landing_id'):
        op.create_index('ix_guest_purchases_landing_id', 'guest_purchases', ['landing_id'])


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_landing_id')

"""Add indexes for gift pending queries and retry on guest_purchases

Adds three indexes:
- (user_id, is_gift, status) for dashboard pending gifts query
- (status, paid_at) for retry_stuck_paid_purchases query
- (buyer_user_id) for FK lookup performance

Revision ID: 0033
Revises: 0032
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0033'
down_revision: Union[str, None] = '0032'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = [
    ('ix_guest_purchases_user_gift_status', ['user_id', 'is_gift', 'status']),
    ('ix_guest_purchases_status_paid_at', ['status', 'paid_at']),
    ('ix_guest_purchases_buyer_user_id', ['buyer_user_id']),
]


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    for index_name, columns in INDEXES:
        if not _has_index('guest_purchases', index_name):
            op.create_index(index_name, 'guest_purchases', columns)


def downgrade() -> None:
    for index_name, _ in reversed(INDEXES):
        if _has_index('guest_purchases', index_name):
            op.drop_index(index_name, table_name='guest_purchases')

"""add composite index on guest_purchases for stats queries

Replaces the single-column ix_guest_purchases_landing_id with a composite
index (landing_id, status, paid_at) that covers the summary, daily, and
tariff breakdown queries in the landing stats endpoint.

Revision ID: 0029
Revises: 0028
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: Union[str, None] = '0028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_index('guest_purchases', 'ix_guest_purchases_landing_status_paid'):
        op.create_index(
            'ix_guest_purchases_landing_status_paid',
            'guest_purchases',
            ['landing_id', 'status', 'paid_at'],
        )
    if _has_index('guest_purchases', 'ix_guest_purchases_landing_id'):
        op.drop_index('ix_guest_purchases_landing_id', 'guest_purchases')


def downgrade() -> None:
    op.create_index('ix_guest_purchases_landing_id', 'guest_purchases', ['landing_id'])
    op.drop_index('ix_guest_purchases_landing_status_paid', 'guest_purchases')

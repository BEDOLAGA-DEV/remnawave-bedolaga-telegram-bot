"""add receipt_uuid and receipt_created_at to guest_purchases

Revision ID: 0045
Revises: 0044
Create Date: 2026-03-21

Adds receipt_uuid and receipt_created_at columns to guest_purchases table
so that NaloGO fiscal receipt UUIDs are persisted on the purchase record
itself (not only on transaction or in Redis). This provides a persistent
DB-level dedup guard and audit trail for receipts created in the
PENDING_ACTIVATION path and code-only gift path where no Transaction
exists at receipt creation time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0045'
down_revision: str | None = '0044'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('guest_purchases', sa.Column('receipt_uuid', sa.String(255), nullable=True))
    op.add_column('guest_purchases', sa.Column('receipt_created_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_guest_purchases_receipt_uuid', 'guest_purchases', ['receipt_uuid'])


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_receipt_uuid', table_name='guest_purchases')
    op.drop_column('guest_purchases', 'receipt_created_at')
    op.drop_column('guest_purchases', 'receipt_uuid')

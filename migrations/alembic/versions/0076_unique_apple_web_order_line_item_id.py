"""add apple iap idempotency and backlog indexes

Revision ID: 0076
Revises: 0075
Create Date: 2026-05-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0076'
down_revision: Union[str, None] = '0075'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    web_order_duplicates = bind.execute(
        sa.text(
            """
            SELECT web_order_line_item_id, COUNT(*) AS count
            FROM apple_transactions
            WHERE web_order_line_item_id IS NOT NULL
            GROUP BY web_order_line_item_id
            HAVING COUNT(*) > 1
            LIMIT 5
            """
        )
    ).mappings().all()
    if web_order_duplicates:
        sample = ', '.join(f'{row["web_order_line_item_id"]} ({row["count"]})' for row in web_order_duplicates)
        raise RuntimeError(
            'Cannot create unique Apple IAP web_order_line_item_id index; '
            f'duplicate values exist: {sample}'
        )

    payload_hash_duplicates = bind.execute(
        sa.text(
            """
            SELECT payload_hash, COUNT(*) AS count
            FROM apple_notifications
            GROUP BY payload_hash
            HAVING COUNT(*) > 1
            LIMIT 5
            """
        )
    ).mappings().all()
    if payload_hash_duplicates:
        sample = ', '.join(f'{row["payload_hash"]} ({row["count"]})' for row in payload_hash_duplicates)
        raise RuntimeError(
            'Cannot create unique Apple IAP notification payload_hash index; '
            f'duplicate values exist: {sample}'
        )

    op.drop_index('ix_apple_transactions_web_order_line_item_id', table_name='apple_transactions')
    op.create_index(
        'ix_apple_transactions_web_order_line_item_id',
        'apple_transactions',
        ['web_order_line_item_id'],
        unique=True,
    )
    op.create_index(
        'ix_apple_transactions_signed_transaction_hash',
        'apple_transactions',
        ['signed_transaction_hash'],
    )
    op.create_index(
        'ix_apple_notifications_payload_hash',
        'apple_notifications',
        ['payload_hash'],
        unique=True,
    )
    op.create_index(
        'ix_apple_notifications_status_received_at',
        'apple_notifications',
        ['status', 'received_at'],
        postgresql_where=sa.text("status IN ('received','failed')"),
    )


def downgrade() -> None:
    op.drop_index('ix_apple_notifications_status_received_at', table_name='apple_notifications')
    op.drop_index('ix_apple_notifications_payload_hash', table_name='apple_notifications')
    op.drop_index('ix_apple_transactions_signed_transaction_hash', table_name='apple_transactions')
    op.drop_index('ix_apple_transactions_web_order_line_item_id', table_name='apple_transactions')
    op.create_index('ix_apple_transactions_web_order_line_item_id', 'apple_transactions', ['web_order_line_item_id'])

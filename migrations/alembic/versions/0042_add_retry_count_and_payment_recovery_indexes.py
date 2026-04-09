"""add retry_count to guest_purchases and expression indexes for payment recovery

Revision ID: 0042
Revises: 0041
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0042'
down_revision: Union[str, None] = '0041'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Payment tables with metadata_json + is_paid column.
_TABLES_WITH_IS_PAID = [
    'yookassa_payments',
    'mulenpay_payments',
    'pal24_payments',
    'wata_payments',
    'platega_payments',
    'cloudpayments_payments',
    'freekassa_payments',
    'kassa_ai_payments',
    'riopay_payments',
    'severpay_payments',
]

# All tables that get a metadata purchase_token index (for downgrade)
_ALL_METADATA_TABLES = [*_TABLES_WITH_IS_PAID, 'heleket_payments']


def upgrade() -> None:
    # 1. Add retry_count column to guest_purchases (idempotent: skip if exists)
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'guest_purchases' AND column_name = 'retry_count'"
        )
    )
    if not result.fetchone():
        op.add_column(
            'guest_purchases',
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        )

    # 2. Create expression indexes for payment recovery queries.
    #    These allow efficient lookup of succeeded payments by purchase_token
    #    stored inside the metadata_json column.
    with op.get_context().autocommit_block():
        # Tables with is_paid boolean column
        for table in _TABLES_WITH_IS_PAID:
            idx_name = f'ix_{table}_metadata_purchase_token'
            op.execute(
                sa.text(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name} '
                    f"ON {table} ((metadata_json ->> 'purchase_token')) "
                    f'WHERE is_paid = TRUE'
                )
            )

        # Heleket: no is_paid column (it's a Python @property), use status filter
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_heleket_payments_metadata_purchase_token '
                "ON heleket_payments ((metadata_json ->> 'purchase_token')) "
                "WHERE status IN ('paid', 'paid_over')"
            )
        )

        # CryptoBot: payload (text) column with JSON inside, no metadata_json.
        # Filter payload LIKE '{%' to skip non-JSON values (e.g. "balance_2_10000").
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cryptobot_payments_payload_purchase_token '
                "ON cryptobot_payments ((CAST(payload AS json) ->> 'purchase_token')) "
                "WHERE status = 'paid' AND payload LIKE '{%'"
            )
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table in _ALL_METADATA_TABLES:
            idx_name = f'ix_{table}_metadata_purchase_token'
            op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS {idx_name}'))

        op.execute(
            sa.text('DROP INDEX CONCURRENTLY IF EXISTS ix_cryptobot_payments_payload_purchase_token')
        )

    op.drop_column('guest_purchases', 'retry_count')

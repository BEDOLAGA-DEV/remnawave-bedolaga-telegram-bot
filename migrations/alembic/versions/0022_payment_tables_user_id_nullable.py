"""make user_id nullable in all payment tables for guest purchases

Extends 0019 (yookassa only) to all payment providers that need to
support guest (landing-page) purchases where no user exists yet.

Revision ID: 0022
Revises: 0021
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: Union[str, None] = '0021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = [
    'cryptobot_payments',
    'heleket_payments',
    'mulenpay_payments',
    'pal24_payments',
    'wata_payments',
    'platega_payments',
    'cloudpayments_payments',
    'freekassa_payments',
    'kassa_ai_payments',
]


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    for table in _TABLES:
        if _table_exists(table):
            op.alter_column(table, 'user_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # WARNING: Will fail if any rows have user_id=NULL (guest payments).
    # Backfill required before downgrading.
    for table in _TABLES:
        if _table_exists(table):
            op.alter_column(table, 'user_id', existing_type=sa.Integer(), nullable=False)

"""add unique constraint on transactions(external_id, payment_method)

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-06

Prevents duplicate transaction records for the same payment provider
external ID, which could cause double-crediting of user balance.
NULL external_id values do not violate the constraint in PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(table: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table "
            "AND constraint_name = :name "
            "AND constraint_type = 'UNIQUE'"
        ),
        {'table': table, 'name': constraint_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # Deduplicate any existing rows with same (external_id, payment_method)
    # where external_id is not NULL. Keep the row with the lowest id,
    # suffix duplicates with _dup_{id} to preserve audit trail.
    op.execute("""
        UPDATE transactions
        SET external_id = external_id || '_dup_' || id::text
        WHERE external_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM transactions
              WHERE external_id IS NOT NULL
              GROUP BY external_id, payment_method
          )
    """)

    if not _constraint_exists('transactions', 'uq_transaction_external_id_method'):
        op.create_unique_constraint(
            'uq_transaction_external_id_method',
            'transactions',
            ['external_id', 'payment_method'],
        )


def downgrade() -> None:
    op.drop_constraint('uq_transaction_external_id_method', 'transactions', type_='unique')

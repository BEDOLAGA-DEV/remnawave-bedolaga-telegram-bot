"""make yookassa_payments.user_id nullable for guest payments

Revision ID: 0019
Revises: 0018
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    if _table_exists('yookassa_payments'):
        op.alter_column('yookassa_payments', 'user_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # WARNING: Will fail if any rows have user_id=NULL (guest payments).
    # Backfill required: UPDATE yookassa_payments SET user_id = 0 WHERE user_id IS NULL;
    if _table_exists('yookassa_payments'):
        op.alter_column('yookassa_payments', 'user_id', existing_type=sa.Integer(), nullable=False)

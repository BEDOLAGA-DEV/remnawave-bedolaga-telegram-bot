"""add YooKassa scope columns

Revision ID: 0090
Revises: 0089
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0090'
down_revision: Union[str, None] = '0089'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('yookassa_payments', sa.Column('yookassa_scope', sa.String(length=32), nullable=True))
    op.create_index(
        'ix_yookassa_payments_scope_payment_id',
        'yookassa_payments',
        ['yookassa_scope', 'yookassa_payment_id'],
        unique=False,
    )
    op.add_column('saved_payment_methods', sa.Column('yookassa_scope', sa.String(length=32), nullable=True))
    op.create_index(
        'ix_saved_payment_methods_user_scope_active',
        'saved_payment_methods',
        ['user_id', 'yookassa_scope', 'is_active'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_saved_payment_methods_user_scope_active', table_name='saved_payment_methods')
    op.drop_column('saved_payment_methods', 'yookassa_scope')
    op.drop_index('ix_yookassa_payments_scope_payment_id', table_name='yookassa_payments')
    op.drop_column('yookassa_payments', 'yookassa_scope')

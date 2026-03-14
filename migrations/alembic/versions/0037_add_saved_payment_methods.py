"""add saved_payment_methods table for recurrent payments

Revision ID: 0037
Revises: 0036
Create Date: 2026-03-05

Adds saved_payment_methods table for storing YooKassa saved payment methods
(bank cards) that can be used for recurring automatic balance top-ups.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0037'
down_revision: Union[str, None] = '0036'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'saved_payment_methods' not in inspector.get_table_names():
        op.create_table(
            'saved_payment_methods',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
            sa.Column('yookassa_payment_method_id', sa.String(255), nullable=False, unique=True, index=True),
            sa.Column('method_type', sa.String(50), nullable=False, server_default='bank_card'),
            sa.Column('card_first6', sa.String(6), nullable=True),
            sa.Column('card_last4', sa.String(4), nullable=True),
            sa.Column('card_type', sa.String(50), nullable=True),
            sa.Column('card_expiry_month', sa.String(2), nullable=True),
            sa.Column('card_expiry_year', sa.String(4), nullable=True),
            sa.Column('title', sa.String(255), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default=sa.True_(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            'ix_saved_payment_methods_user_active',
            'saved_payment_methods',
            ['user_id', 'is_active'],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'saved_payment_methods' in inspector.get_table_names():
        op.drop_table('saved_payment_methods')

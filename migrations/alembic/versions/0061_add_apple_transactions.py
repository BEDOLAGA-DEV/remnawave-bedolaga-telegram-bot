"""add apple_transactions table

Revision ID: 0061
Revises: 0060
Create Date: 2026-04-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0061'
down_revision: Union[str, None] = '0060'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'apple_transactions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('transaction_id', sa.String(64), unique=True, nullable=False),
        sa.Column('original_transaction_id', sa.String(64), nullable=True),
        sa.Column('product_id', sa.String(128), nullable=False),
        sa.Column('bundle_id', sa.String(255), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('environment', sa.String(16), nullable=False),
        sa.Column('status', sa.String(50), server_default='verified'),
        sa.Column('is_paid', sa.Boolean(), server_default=sa.text('true')),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('transaction_id_fk', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index('ix_apple_transactions_transaction_id', 'apple_transactions', ['transaction_id'])
    op.create_index('ix_apple_transactions_original_transaction_id', 'apple_transactions', ['original_transaction_id'])
    op.create_index('ix_apple_transactions_user_id', 'apple_transactions', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_apple_transactions_user_id', table_name='apple_transactions')
    op.drop_index('ix_apple_transactions_original_transaction_id', table_name='apple_transactions')
    op.drop_index('ix_apple_transactions_transaction_id', table_name='apple_transactions')
    op.drop_table('apple_transactions')

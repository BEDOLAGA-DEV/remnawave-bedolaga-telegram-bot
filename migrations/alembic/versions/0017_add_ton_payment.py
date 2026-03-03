"""add ton_payments table

Revision ID: 0017
Revises: 0014
Create Date: 2026-03-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    conn = op.get_bind()
    return sa.inspect(conn).has_table(table_name)


def upgrade() -> None:
    if _has_table('ton_payments'):
        return

    op.create_table(
        'ton_payments',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('memo', sa.String(64), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('amount_nano', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(50), nullable=False, server_default='pending'),
        sa.Column('ton_hash', sa.String(64), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('memo', name='uq_ton_payments_memo'),
        sa.UniqueConstraint('ton_hash', name='uq_ton_payments_ton_hash'),
    )

    op.create_index('ix_ton_payments_id', 'ton_payments', ['id'])
    op.create_index('ix_ton_payments_memo', 'ton_payments', ['memo'])


def downgrade() -> None:
    op.drop_index('ix_ton_payments_memo', table_name='ton_payments')
    op.drop_index('ix_ton_payments_id', table_name='ton_payments')
    op.drop_table('ton_payments')

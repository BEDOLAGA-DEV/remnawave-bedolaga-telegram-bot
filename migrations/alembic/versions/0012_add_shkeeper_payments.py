"""add shkeeper payments table

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shkeeper_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('shkeeper_invoice_id', sa.String(length=64), nullable=True),
        sa.Column('external_id', sa.String(length=64), nullable=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('amount_crypto', sa.String(length=64), nullable=True),
        sa.Column('crypto', sa.String(length=32), nullable=True),
        sa.Column('display_amount', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='new'),
        sa.Column('is_paid', sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('success_url', sa.Text(), nullable=True),
        sa.Column('fail_url', sa.Text(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_shkeeper_payments_external_id'), 'shkeeper_payments', ['external_id'], unique=False)
    op.create_index(op.f('ix_shkeeper_payments_id'), 'shkeeper_payments', ['id'], unique=False)
    op.create_index(op.f('ix_shkeeper_payments_order_id'), 'shkeeper_payments', ['order_id'], unique=True)
    op.create_index(
        op.f('ix_shkeeper_payments_shkeeper_invoice_id'),
        'shkeeper_payments',
        ['shkeeper_invoice_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_shkeeper_payments_shkeeper_invoice_id'), table_name='shkeeper_payments')
    op.drop_index(op.f('ix_shkeeper_payments_order_id'), table_name='shkeeper_payments')
    op.drop_index(op.f('ix_shkeeper_payments_id'), table_name='shkeeper_payments')
    op.drop_index(op.f('ix_shkeeper_payments_external_id'), table_name='shkeeper_payments')
    op.drop_table('shkeeper_payments')

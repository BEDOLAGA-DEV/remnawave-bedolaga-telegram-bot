"""add robokassa_payments table

Revision ID: 0059
Revises: 0058
Create Date: 2026-04-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0059'
down_revision: Union[str, None] = '0058'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'robokassa_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('inv_id', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False, server_default='RUB'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(50), nullable=False, server_default='created'),
        sa.Column('is_paid', sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('inc_curr_label', sa.String(64), nullable=True),
        sa.Column('robokassa_op_id', sa.String(128), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_robokassa_payments_id', 'robokassa_payments', ['id'])
    op.create_index('ix_robokassa_payments_inv_id', 'robokassa_payments', ['inv_id'], unique=True)
    op.create_index('ix_robokassa_payments_robokassa_op_id', 'robokassa_payments', ['robokassa_op_id'])


def downgrade() -> None:
    op.drop_index('ix_robokassa_payments_robokassa_op_id', table_name='robokassa_payments')
    op.drop_index('ix_robokassa_payments_inv_id', table_name='robokassa_payments')
    op.drop_index('ix_robokassa_payments_id', table_name='robokassa_payments')
    op.drop_table('robokassa_payments')

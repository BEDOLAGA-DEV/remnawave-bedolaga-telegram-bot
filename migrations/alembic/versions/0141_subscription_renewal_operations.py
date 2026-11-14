"""Persist renewal responses atomically with balance charges.

Revision ID: 0141
Revises: 0140
"""

import sqlalchemy as sa
from alembic import op


revision = '0141'
down_revision = '0140'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'subscription_renewal_operations',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('operation_id', sa.String(36), primary_key=True),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('expected_end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('subscription_renewal_operations')

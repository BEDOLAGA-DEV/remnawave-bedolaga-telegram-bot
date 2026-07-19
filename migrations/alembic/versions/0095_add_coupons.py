"""add coupon_batches and coupons tables

Wholesale/partner sales: the admin batch-generates one-time coupons
(``coupon_batches`` holds tariff+period+bookkeeping, ``coupons`` holds the
per-link secret token). Redeeming a coupon via the ``/start coupon_<token>``
deep link grants a new subscription for the batch period or extends an
existing one. ON DELETE CASCADE ties coupons to their batch.

Revision ID: 0095
Revises: 0094
Create Date: 2026-07-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0095'
down_revision: Union[str, None] = '0094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'coupon_batches' not in tables:
        op.create_table(
            'coupon_batches',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('tariff_id', sa.Integer(), nullable=True),
            sa.Column('period_days', sa.Integer(), nullable=False),
            sa.Column('coupons_total', sa.Integer(), nullable=False),
            sa.Column('wholesale_price_kopeks', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
            sa.Column('is_revoked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['tariff_id'], ['tariffs.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_coupon_batches_tariff_id', 'coupon_batches', ['tariff_id'])

    if 'coupons' not in tables:
        op.create_table(
            'coupons',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('batch_id', sa.Integer(), nullable=False),
            sa.Column('token', sa.String(length=64), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
            sa.Column('redeemed_by', sa.Integer(), nullable=True),
            sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['batch_id'], ['coupon_batches.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['redeemed_by'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
        # Column(unique=True, index=True) in the model → a single unique index
        op.create_index('ix_coupons_token', 'coupons', ['token'], unique=True)
        op.create_index('ix_coupons_redeemed_by', 'coupons', ['redeemed_by'])
        op.create_index('ix_coupons_batch_status', 'coupons', ['batch_id', 'status'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'coupons' in tables:
        op.drop_index('ix_coupons_batch_status', table_name='coupons')
        op.drop_index('ix_coupons_redeemed_by', table_name='coupons')
        op.drop_index('ix_coupons_token', table_name='coupons')
        op.drop_table('coupons')

    if 'coupon_batches' in tables:
        op.drop_index('ix_coupon_batches_tariff_id', table_name='coupon_batches')
        op.drop_table('coupon_batches')

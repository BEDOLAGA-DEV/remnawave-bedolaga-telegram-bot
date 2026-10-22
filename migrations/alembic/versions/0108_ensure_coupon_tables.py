"""ensure coupon_batches and coupons tables exist (catch-up for slig/production-patches)

On slig/production-patches the revision IDs 0094-0096 were used by custom
branch-specific migrations (antilopay recurrents, ensure_columns, referrer_id)
BEFORE the upstream bot added the same revision IDs for different features.

As a result, deployments that ran the old slig migrations 0094-0096 never ran
the upstream 0095 that creates coupon_batches/coupons. Migration 0102 (which
adds max_per_user to coupon_batches) is also idempotent and skips when the
table is absent.

This catch-up migration repairs those databases once and for all:
  - Creates coupon_batches (with max_per_user already included) if absent.
  - Creates coupons if absent.

Idempotent: guarded by inspector — no-op on databases that already have these
tables (fresh installs and non-slig deployments).

Revision ID: 0108
Revises: 0107c
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0108'
down_revision: Union[str, None] = '0107c'
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
            # max_per_user: добавлен upstream-миграцией 0102; включаем сразу,
            # чтобы 0102 (idempotent) корректно пропустил ALTER TABLE.
            sa.Column('max_per_user', sa.Integer(), nullable=False, server_default='0'),
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

"""add wl traffic fields

Revision ID: 0055
Revises: 0054
Create Date: 2026-03-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0055'
down_revision: Union[str, None] = '0054'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Idempotent: some installs already have these columns/table from an
    # earlier custom migration branch. Check before adding.
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    existing_cols = {c['name'] for c in inspector.get_columns('subscriptions')}

    new_cols = {
        'wl_traffic_limit_gb': sa.Column('wl_traffic_limit_gb', sa.Integer(), nullable=True),
        'wl_traffic_used_gb': sa.Column('wl_traffic_used_gb', sa.Float(), nullable=True),
        'wl_purchased_traffic_gb': sa.Column('wl_purchased_traffic_gb', sa.Integer(), nullable=True),
        'wl_traffic_reset_at': sa.Column('wl_traffic_reset_at', sa.DateTime(timezone=True), nullable=True),
    }
    for name, column in new_cols.items():
        if name not in existing_cols:
            op.add_column('subscriptions', column)

    # Backfill defaults only for NULL rows (idempotent).
    op.execute(
        "UPDATE subscriptions SET "
        "wl_traffic_limit_gb = COALESCE(wl_traffic_limit_gb, 0), "
        "wl_traffic_used_gb = COALESCE(wl_traffic_used_gb, 0.0), "
        "wl_purchased_traffic_gb = COALESCE(wl_purchased_traffic_gb, 0)"
    )

    if 'wl_traffic_purchases' not in inspector.get_table_names():
        op.create_table(
            'wl_traffic_purchases',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('subscription_id', sa.Integer(), nullable=False),
            sa.Column('traffic_gb', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_wl_traffic_purchases_created_at'), 'wl_traffic_purchases', ['created_at'], unique=False)
        op.create_index(op.f('ix_wl_traffic_purchases_expires_at'), 'wl_traffic_purchases', ['expires_at'], unique=False)
        op.create_index(op.f('ix_wl_traffic_purchases_id'), 'wl_traffic_purchases', ['id'], unique=False)
        op.create_index(op.f('ix_wl_traffic_purchases_subscription_id'), 'wl_traffic_purchases', ['subscription_id'], unique=False)

def downgrade() -> None:
    # Drop table
    op.drop_index(op.f('ix_wl_traffic_purchases_subscription_id'), table_name='wl_traffic_purchases')
    op.drop_index(op.f('ix_wl_traffic_purchases_id'), table_name='wl_traffic_purchases')
    op.drop_index(op.f('ix_wl_traffic_purchases_expires_at'), table_name='wl_traffic_purchases')
    op.drop_index(op.f('ix_wl_traffic_purchases_created_at'), table_name='wl_traffic_purchases')
    op.drop_table('wl_traffic_purchases')

    # Drop columns
    op.drop_column('subscriptions', 'wl_traffic_reset_at')
    op.drop_column('subscriptions', 'wl_purchased_traffic_gb')
    op.drop_column('subscriptions', 'wl_traffic_used_gb')
    op.drop_column('subscriptions', 'wl_traffic_limit_gb')

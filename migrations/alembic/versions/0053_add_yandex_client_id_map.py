"""add yandex_client_id_map table + guest_purchases.yandex_cid column

Revision ID: 0053
Revises: 0052
Create Date: 2026-03-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0053'
down_revision: Union[str, None] = '0052'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create table only if it does not already exist (may have been created by earlier patch branch)
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'yandex_client_id_map')")
    )
    if not result.scalar():
        op.create_table(
            'yandex_client_id_map',
            sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False),
            sa.Column('yandex_cid', sa.String(128), nullable=False),
            sa.Column('source', sa.String(20), nullable=False, server_default='web'),
            sa.Column('counter_id', sa.String(32), nullable=True),
            sa.Column('registration_sent', sa.Boolean, server_default=sa.text('false'), nullable=False),
            sa.Column('trial_sent', sa.Boolean, server_default=sa.text('false'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # Add yandex_cid column to guest_purchases if not present
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'guest_purchases' AND column_name = 'yandex_cid')"
        )
    )
    if not result.scalar():
        op.add_column('guest_purchases', sa.Column('yandex_cid', sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column('guest_purchases', 'yandex_cid')
    op.drop_table('yandex_client_id_map')

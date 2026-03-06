"""add yandex_client_id_map table for offline conversions

Revision ID: 0029
Revises: 0028
Create Date: 2026-03-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: Union[str, None] = '0028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'yandex_client_id_map',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False),
        sa.Column('yandex_cid', sa.String(128), nullable=False),
        sa.Column('source', sa.String(10), nullable=False, server_default='web'),
        sa.Column('counter_id', sa.String(32), nullable=True),
        sa.Column('registration_sent', sa.Boolean, server_default=sa.text('false'), nullable=False),
        sa.Column('trial_sent', sa.Boolean, server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_yandex_cid_user_id', 'yandex_client_id_map', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_yandex_cid_user_id', table_name='yandex_client_id_map')
    op.drop_table('yandex_client_id_map')

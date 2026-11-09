"""add device_blocks table

Hard HWID device blocking, enforced by the hwid-blocker middleware between
nginx and the subscription page. A blocked device receives an empty profile
plus an announce message instead of a usable subscription.

Scope is per-(user, hwid); admin bans carry user_id=NULL so a global block
can coexist with per-user blocks (Postgres unique treats NULLs as distinct).
ON DELETE CASCADE on user_id ties the user-scoped rows to the account.

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0141'
down_revision: Union[str, None] = '0140'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'device_blocks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('hwid', sa.String(length=64), nullable=False),
        sa.Column('remnawave_uuid', sa.String(length=255), nullable=True),
        sa.Column('device_label', sa.String(length=255), nullable=True),
        sa.Column('platform', sa.String(length=64), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column(
            'banned_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('banned_by', sa.String(length=20), server_default='user', nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'hwid', name='uq_device_blocks_user_hwid'),
    )
    op.create_index('ix_device_blocks_user_id', 'device_blocks', ['user_id'])
    op.create_index('ix_device_blocks_hwid', 'device_blocks', ['hwid'])


def downgrade() -> None:
    op.drop_index('ix_device_blocks_hwid', table_name='device_blocks')
    op.drop_index('ix_device_blocks_user_id', table_name='device_blocks')
    op.drop_table('device_blocks')

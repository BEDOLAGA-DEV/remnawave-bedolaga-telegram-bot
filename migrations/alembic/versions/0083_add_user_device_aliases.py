"""add user_device_aliases table

Per-user local nickname for HWID devices reported by RemnaWave. The alias
is set by the end-user from the bot or cabinet and is shown instead of
the raw platform/deviceModel string. Scope: (user, hwid) — same physical
device shares the alias across a user's subscriptions in multi-tariff
mode. ON DELETE CASCADE on user_id ties lifecycle to the account.

Revision ID: 0083
Revises: 0082
Create Date: 2026-05-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0083'
down_revision: Union[str, None] = '0082'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_device_aliases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('hwid', sa.String(length=255), nullable=False),
        sa.Column('alias', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'hwid', name='uq_user_device_aliases_user_hwid'),
    )
    op.create_index('ix_user_device_aliases_user_id', 'user_device_aliases', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_device_aliases_user_id', table_name='user_device_aliases')
    op.drop_table('user_device_aliases')

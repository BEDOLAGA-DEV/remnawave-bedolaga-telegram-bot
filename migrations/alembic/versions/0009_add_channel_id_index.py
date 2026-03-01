"""add channel_id index for user_channel_subscriptions

Revision ID: 0009
Revises: 0008
Create Date: 2026-02-24
"""

from typing import Sequence, Union

from alembic import op

revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_user_channel_sub_channel_id',
        'user_channel_subscriptions',
        ['channel_id'],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('ix_user_channel_sub_channel_id', table_name='user_channel_subscriptions')

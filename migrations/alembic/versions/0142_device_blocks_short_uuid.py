"""add short_uuid to device_blocks (scope ban per subscription)

A hard HWID block now applies per (user, hwid, short_uuid) instead of per
(user, hwid). `short_uuid` is the Remnawave public subscription key (the
/{key} in the subscription URL). This lets a device banned on one subscription
still connect on a different one (e.g. a freshly purchased one). An empty
short_uuid keeps the old global behaviour (admin raw-hwid bans + legacy rows).

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0142'
down_revision: Union[str, None] = '0141'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('device_blocks', sa.Column('short_uuid', sa.String(length=64), nullable=True))
    op.drop_constraint('uq_device_blocks_user_hwid', 'device_blocks', type_='unique')
    op.create_unique_constraint(
        'uq_device_blocks_user_hwid_short', 'device_blocks', ['user_id', 'hwid', 'short_uuid']
    )


def downgrade() -> None:
    op.drop_constraint('uq_device_blocks_user_hwid_short', 'device_blocks', type_='unique')
    op.create_unique_constraint('uq_device_blocks_user_hwid', 'device_blocks', ['user_id', 'hwid'])
    op.drop_column('device_blocks', 'short_uuid')

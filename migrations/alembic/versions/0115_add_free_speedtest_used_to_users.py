"""add free_speedtest_used to users

Lets a user without an active subscription run the cabinet speedtest exactly
once. After that one free run the speedtest requires an active subscription
again. Defaults to false for all existing users.

Revision ID: 0115
Revises: 0114
Create Date: 2026-06-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0115'
down_revision: Union[str, None] = '0114'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('free_speedtest_used', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('users', 'free_speedtest_used')

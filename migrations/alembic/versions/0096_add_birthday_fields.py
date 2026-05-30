"""add birthday fields to users

Revision ID: 0096
Revises: 0095
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('birth_date', sa.Date(), nullable=True))
    op.add_column('users', sa.Column('birthday_synced_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('birthday_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('last_birthday_reward_year', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_birthday_reward_year')
    op.drop_column('users', 'birthday_changed_at')
    op.drop_column('users', 'birthday_synced_at')
    op.drop_column('users', 'birth_date')

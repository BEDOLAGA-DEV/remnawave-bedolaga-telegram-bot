"""add last_bot_id to users

Stores the Telegram bot id that last interacted with the user.

Revision ID: 0039
Revises: 0038
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: Union[str, None] = '0038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_column('users', 'last_bot_id'):
        op.add_column('users', sa.Column('last_bot_id', sa.BigInteger(), nullable=True))

    if not _has_index('users', 'ix_users_last_bot_id'):
        op.create_index('ix_users_last_bot_id', 'users', ['last_bot_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_users_last_bot_id', table_name='users')
    op.drop_column('users', 'last_bot_id')

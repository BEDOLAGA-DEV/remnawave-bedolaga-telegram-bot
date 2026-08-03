"""add apple oauth provider id to users

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'users'
_COLUMN = 'apple_id'
_INDEX = 'ix_users_apple_id'


def _column_exists(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(col['name'] == _COLUMN for col in inspector.get_columns(_TABLE))


def _index_exists(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(index['name'] == _INDEX for index in inspector.get_indexes(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    if not _column_exists(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=255), nullable=True))
    if not _index_exists(bind):
        op.create_index(_INDEX, _TABLE, [_COLUMN], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _column_exists(bind):
        op.drop_column(_TABLE, _COLUMN)

"""create account deletion requests

Revision ID: 0087
Revises: 0086
Create Date: 2026-05-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0087'
down_revision: Union[str, None] = '0086'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'account_deletion_requests'


def _table_exists(bind: sa.engine.Connection) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def _index_exists(bind: sa.engine.Connection, index_name: str) -> bool:
    if not _table_exists(bind):
        return False
    return any(index['name'] == index_name for index in sa.inspect(bind).get_indexes(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        op.create_table(
            _TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
            sa.Column('panel_uuids', sa.JSON(), nullable=False),
            sa.Column('telegram_id', sa.BigInteger(), nullable=True),
            sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('max_attempts', sa.Integer(), server_default='10', nullable=False),
            sa.Column('next_retry_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _index_exists(bind, 'ix_account_deletion_requests_id'):
        op.create_index('ix_account_deletion_requests_id', _TABLE, ['id'], unique=False)
    if not _index_exists(bind, 'ix_account_deletion_requests_user_id'):
        op.create_index('ix_account_deletion_requests_user_id', _TABLE, ['user_id'], unique=False)
    if not _index_exists(bind, 'ix_account_deletion_requests_status'):
        op.create_index('ix_account_deletion_requests_status', _TABLE, ['status'], unique=False)
    if not _index_exists(bind, 'ix_account_deletion_requests_status_next_retry'):
        op.create_index(
            'ix_account_deletion_requests_status_next_retry',
            _TABLE,
            ['status', 'next_retry_at'],
            unique=False,
        )
    if not _index_exists(bind, 'ix_account_deletion_requests_user_status'):
        op.create_index(
            'ix_account_deletion_requests_user_status',
            _TABLE,
            ['user_id', 'status'],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return

    for index_name in (
        'ix_account_deletion_requests_user_status',
        'ix_account_deletion_requests_status_next_retry',
        'ix_account_deletion_requests_status',
        'ix_account_deletion_requests_user_id',
        'ix_account_deletion_requests_id',
    ):
        if _index_exists(bind, index_name):
            op.drop_index(index_name, table_name=_TABLE)

    op.drop_table(_TABLE)

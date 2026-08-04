"""create oauth provider revocation events

Revision ID: 0089
Revises: 0088
Create Date: 2026-05-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0089'
down_revision: Union[str, None] = '0088'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'oauth_provider_revocation_events'
_ACCOUNT_DELETION_TABLE = 'account_deletion_requests'
_OAUTH_REVOCATION_EVENT_IDS_COLUMN = 'oauth_revocation_event_ids'


def _table_exists(bind: sa.engine.Connection) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def _index_exists(bind: sa.engine.Connection, index_name: str) -> bool:
    if not _table_exists(bind):
        return False
    return any(index['name'] == index_name for index in sa.inspect(bind).get_indexes(_TABLE))


def _column_exists(bind: sa.engine.Connection, table_name: str, column_name: str) -> bool:
    if not sa.inspect(bind).has_table(table_name):
        return False
    return any(column['name'] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, _ACCOUNT_DELETION_TABLE, _OAUTH_REVOCATION_EVENT_IDS_COLUMN):
        op.add_column(
            _ACCOUNT_DELETION_TABLE,
            sa.Column(
                _OAUTH_REVOCATION_EVENT_IDS_COLUMN,
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'::json"),
            ),
        )
        op.alter_column(_ACCOUNT_DELETION_TABLE, _OAUTH_REVOCATION_EVENT_IDS_COLUMN, server_default=None)

    if not _table_exists(bind):
        op.create_table(
            _TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('provider', sa.String(length=32), nullable=False),
            sa.Column('provider_id', sa.String(length=255), nullable=False),
            sa.Column('purpose', sa.String(length=16), nullable=False),
            sa.Column('token_type', sa.String(length=32), nullable=True),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
            sa.CheckConstraint("provider IN ('google', 'apple')", name='ck_oauth_provider_revocation_events_provider'),
            sa.CheckConstraint("purpose IN ('unlink', 'delete')", name='ck_oauth_provider_revocation_events_purpose'),
            sa.CheckConstraint(
                "status IN ('pending', 'succeeded', 'failed')",
                name='ck_oauth_provider_revocation_events_status',
            ),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_oauth_provider_revocation_events_id'), _TABLE, ['id'], unique=False)

    if not _index_exists(bind, 'ix_oauth_provider_revocation_events_user'):
        op.create_index('ix_oauth_provider_revocation_events_user', _TABLE, ['user_id'], unique=False)
    if not _index_exists(bind, 'ix_oauth_provider_revocation_events_provider_status'):
        op.create_index(
            'ix_oauth_provider_revocation_events_provider_status',
            _TABLE,
            ['provider', 'status'],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        pass
    else:
        if _index_exists(bind, 'ix_oauth_provider_revocation_events_provider_status'):
            op.drop_index('ix_oauth_provider_revocation_events_provider_status', table_name=_TABLE)
        if _index_exists(bind, 'ix_oauth_provider_revocation_events_user'):
            op.drop_index('ix_oauth_provider_revocation_events_user', table_name=_TABLE)
        if _index_exists(bind, 'ix_oauth_provider_revocation_events_id'):
            op.drop_index(op.f('ix_oauth_provider_revocation_events_id'), table_name=_TABLE)
        op.drop_table(_TABLE)
    if _column_exists(bind, _ACCOUNT_DELETION_TABLE, _OAUTH_REVOCATION_EVENT_IDS_COLUMN):
        op.drop_column(_ACCOUNT_DELETION_TABLE, _OAUTH_REVOCATION_EVENT_IDS_COLUMN)

"""create cabinet_refresh_tokens table

Revision ID: 0056
Revises: 0055
Create Date: 2026-04-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0056'
down_revision: Union[str, None] = '0055'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return table in inspector.get_table_names()


def _has_index(table: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_table('cabinet_refresh_tokens'):
        op.create_table(
            'cabinet_refresh_tokens',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('token_hash', sa.String(255), nullable=False),
            sa.Column('device_info', sa.String(500), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_index('cabinet_refresh_tokens', 'ix_cabinet_refresh_tokens_id'):
        op.create_index('ix_cabinet_refresh_tokens_id', 'cabinet_refresh_tokens', ['id'])
    if not _has_index('cabinet_refresh_tokens', 'ix_cabinet_refresh_tokens_token_hash'):
        op.create_index('ix_cabinet_refresh_tokens_token_hash', 'cabinet_refresh_tokens', ['token_hash'], unique=True)
    if not _has_index('cabinet_refresh_tokens', 'ix_cabinet_refresh_tokens_user'):
        op.create_index('ix_cabinet_refresh_tokens_user', 'cabinet_refresh_tokens', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_cabinet_refresh_tokens_user', table_name='cabinet_refresh_tokens')
    op.drop_index('ix_cabinet_refresh_tokens_token_hash', table_name='cabinet_refresh_tokens')
    op.drop_index('ix_cabinet_refresh_tokens_id', table_name='cabinet_refresh_tokens')
    op.drop_table('cabinet_refresh_tokens')

"""add bot_admin_roles table

Adds bot-level RBAC: per-user permission sections for Telegram bot admin handlers.

Revision ID: 0064
Revises: 0063
Create Date: 2026-04-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0064'
down_revision: Union[str, None] = '0063'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'bot_admin_roles' in inspector.get_table_names():
        return

    op.create_table(
        'bot_admin_roles',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
            unique=True,
        ),
        sa.Column('permissions', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column(
            'created_by',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_bot_admin_roles_user_id', 'bot_admin_roles', ['user_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_bot_admin_roles_user_id', table_name='bot_admin_roles')
    op.drop_table('bot_admin_roles')

"""add media fields to forum_ticket_messages

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-02

Добавляет поля media_type и media_file_id в forum_ticket_messages
для поддержки фото-вложений в AI-тикетах.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    if not _has_column('forum_ticket_messages', 'media_type'):
        op.add_column('forum_ticket_messages', sa.Column('media_type', sa.String(50), nullable=True))
    if not _has_column('forum_ticket_messages', 'media_file_id'):
        op.add_column('forum_ticket_messages', sa.Column('media_file_id', sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column('forum_ticket_messages', 'media_file_id')
    op.drop_column('forum_ticket_messages', 'media_type')

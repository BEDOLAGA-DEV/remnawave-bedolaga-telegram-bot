"""add source_chat_id and source_message_id to user_reviews

Allows real Telegram forward_message instead of re-sending text.

Revision ID: 0074
Revises: 0073
Create Date: 2026-04-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0074'
down_revision: Union[str, None] = '0073'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'user_reviews' not in inspector.get_table_names():
        return

    existing = {c['name'] for c in inspector.get_columns('user_reviews')}

    if 'source_chat_id' not in existing:
        op.add_column(
            'user_reviews',
            sa.Column('source_chat_id', sa.BigInteger(), nullable=True),
        )

    if 'source_message_id' not in existing:
        op.add_column(
            'user_reviews',
            sa.Column('source_message_id', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('user_reviews', 'source_message_id')
    op.drop_column('user_reviews', 'source_chat_id')

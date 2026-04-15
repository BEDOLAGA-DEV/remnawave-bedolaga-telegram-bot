"""add ticket_messages.media_items for multi-media bubbles

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'ticket_messages' AND column_name = 'media_items')"
        )
    )
    if not result.scalar():
        op.add_column(
            "ticket_messages",
            sa.Column(
                "media_items",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'ticket_messages' AND column_name = 'media_items')"
        )
    )
    if result.scalar():
        op.drop_column("ticket_messages", "media_items")

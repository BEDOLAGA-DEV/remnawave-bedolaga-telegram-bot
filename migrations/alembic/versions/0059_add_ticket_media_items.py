"""add ticket_messages.media_items for multi-media bubbles

Revision ID: 0059
Revises: 0058
Create Date: 2026-04-15

Depends on PR #2851 (migration 0058 — yandex_client_id_map) being merged first.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059"
down_revision: Union[str, None] = "0058"
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

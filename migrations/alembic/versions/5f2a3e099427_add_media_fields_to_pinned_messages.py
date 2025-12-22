"""add media fields to pinned messages"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5f2a3e099427"
down_revision: Union[str, None] = "c9c71d04f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "pinned_messages"


def _table_exists(inspector: sa.Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def _column_missing(inspector: sa.Inspector, column_name: str) -> bool:
    columns = {column.get("name") for column in inspector.get_columns(TABLE_NAME)}
    return column_name not in columns


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        return

    if _column_missing(inspector, "media_type"):
        op.add_column(
            TABLE_NAME,
            sa.Column("media_type", sa.String(length=32), nullable=True),
        )

    if _column_missing(inspector, "media_file_id"):
        op.add_column(
            TABLE_NAME,
            sa.Column("media_file_id", sa.String(length=255), nullable=True),
        )

    # Ensure content has a default value for media-only messages
    op.alter_column(
        TABLE_NAME,
        "content",
        existing_type=sa.Text(),
        nullable=False,
        server_default="",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        return

    if not _column_missing(inspector, "media_type"):
        op.drop_column(TABLE_NAME, "media_type")

    if not _column_missing(inspector, "media_file_id"):
        op.drop_column(TABLE_NAME, "media_file_id")

    op.alter_column(
        TABLE_NAME,
        "content",
        existing_type=sa.Text(),
        nullable=False,
        server_default=None,
    )

"""add yandex_client_id_map table + guest_purchases offline conv columns

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) yandex_client_id_map — created idempotently
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'yandex_client_id_map')"
        )
    )
    if not result.scalar():
        op.create_table(
            "yandex_client_id_map",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "user_id",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                unique=True,
                nullable=False,
            ),
            sa.Column("yandex_cid", sa.String(128), nullable=False),
            sa.Column("source", sa.String(20), nullable=False, server_default="web"),
            sa.Column("counter_id", sa.String(32), nullable=True),
            sa.Column(
                "registration_sent",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "trial_sent",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("subid", sa.String(255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )

    # 2) guest_purchases — add yandex_cid / subid / referrer (idempotent)
    for col_name, col_def in (
        ("yandex_cid", sa.Column("yandex_cid", sa.String(128), nullable=True)),
        ("subid", sa.Column("subid", sa.String(255), nullable=True)),
        ("referrer", sa.Column("referrer", sa.String(500), nullable=True)),
    ):
        result = conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'guest_purchases' AND column_name = :col)"
            ),
            {"col": col_name},
        )
        if not result.scalar():
            op.add_column("guest_purchases", col_def)


def downgrade() -> None:
    conn = op.get_bind()
    for col_name in ("referrer", "subid", "yandex_cid"):
        result = conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'guest_purchases' AND column_name = :col)"
            ),
            {"col": col_name},
        )
        if result.scalar():
            op.drop_column("guest_purchases", col_name)

    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'yandex_client_id_map')"
        )
    )
    if result.scalar():
        op.drop_table("yandex_client_id_map")

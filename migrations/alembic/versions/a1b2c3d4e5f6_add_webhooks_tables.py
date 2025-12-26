"""Add webhooks and webhook_deliveries tables"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e3c1e0b5b4a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WEBHOOKS_TABLE = "webhooks"
DELIVERIES_TABLE = "webhook_deliveries"


def _table_exists(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Создаем таблицу webhooks
    if not _table_exists(inspector, WEBHOOKS_TABLE):
        op.create_table(
            WEBHOOKS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("secret", sa.String(length=128), nullable=True),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        )

        op.create_index("ix_webhooks_event_type", WEBHOOKS_TABLE, ["event_type"])
        op.create_index("ix_webhooks_is_active", WEBHOOKS_TABLE, ["is_active"])

    # Создаем таблицу webhook_deliveries
    if not _table_exists(inspector, DELIVERIES_TABLE):
        op.create_table(
            DELIVERIES_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "webhook_id",
                sa.Integer(),
                sa.ForeignKey("webhooks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        )

        op.create_index(
            "ix_webhook_deliveries_webhook_created",
            DELIVERIES_TABLE,
            ["webhook_id", "created_at"],
        )
        op.create_index("ix_webhook_deliveries_status", DELIVERIES_TABLE, ["status"])
        op.create_index("ix_webhook_deliveries_webhook_id", DELIVERIES_TABLE, ["webhook_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Удаляем таблицу webhook_deliveries
    if _table_exists(inspector, DELIVERIES_TABLE):
        op.drop_index("ix_webhook_deliveries_webhook_id", table_name=DELIVERIES_TABLE)
        op.drop_index("ix_webhook_deliveries_status", table_name=DELIVERIES_TABLE)
        op.drop_index(
            "ix_webhook_deliveries_webhook_created",
            table_name=DELIVERIES_TABLE,
        )
        op.drop_table(DELIVERIES_TABLE)

    # Удаляем таблицу webhooks
    if _table_exists(inspector, WEBHOOKS_TABLE):
        op.drop_index("ix_webhooks_is_active", table_name=WEBHOOKS_TABLE)
        op.drop_index("ix_webhooks_event_type", table_name=WEBHOOKS_TABLE)
        op.drop_table(WEBHOOKS_TABLE)


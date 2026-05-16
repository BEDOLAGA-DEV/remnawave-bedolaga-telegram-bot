"""drop redundant ix_traffic_purchases_subscription_id (covered by composite)

Composite `ix_traffic_purchases_sub_expires(subscription_id, expires_at)` из 0080
покрывает leftmost-prefix-запросы `WHERE subscription_id = X`, поэтому single-column
`ix_traffic_purchases_subscription_id` стал избыточным.

Дроп вынесен в отдельную ревизию: если упадёт здесь, прогресс по 0080 (CREATE composite)
уже зафиксирован в alembic_version и не теряется.

Revision ID: 0081
Revises: 0080
Create Date: 2026-05-13

"""

from typing import Sequence, Union

from alembic import op


revision: str = '0081'
down_revision: Union[str, None] = '0080'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_traffic_purchases_subscription_id')
    else:
        try:
            op.drop_index('ix_traffic_purchases_subscription_id', table_name='traffic_purchases')
        except Exception:  # noqa: BLE001
            # SQLite/dev DB может не иметь этого индекса — норм
            pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_traffic_purchases_subscription_id '
                'ON traffic_purchases (subscription_id)'
            )
    else:
        op.create_index(
            'ix_traffic_purchases_subscription_id',
            'traffic_purchases',
            ['subscription_id'],
            unique=False,
        )

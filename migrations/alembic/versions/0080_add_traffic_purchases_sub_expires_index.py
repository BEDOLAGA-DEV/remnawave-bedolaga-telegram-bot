"""add composite index (subscription_id, expires_at) on traffic_purchases

Housekeeping-запросы в `_housekeep_expired_purchases` и
`_apply_base_limit_preserving_active_purchases` фильтруют по обоим колонкам:
    WHERE subscription_id = :id AND expires_at <op> :now

До этой миграции были только single-column индексы (`subscription_id`,
`expires_at`), PostgreSQL выбирал один и фильтровал по второму через scan.
На активных юзерах с большим числом докупок это становилось горячим.

CREATE INDEX CONCURRENTLY чтобы не лочить таблицу на проде.

Сплит на 2 ревизии — дроп редундантного single-column `ix_traffic_purchases_subscription_id`
вынесен в 0081, чтобы при сбое DROP не блокировался прогресс alembic_version
после успешного CREATE.

Revision ID: 0080
Revises: 0079
Create Date: 2026-05-13

"""

from typing import Sequence, Union

from alembic import op


revision: str = '0080'
down_revision: Union[str, None] = '0079'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        # CREATE INDEX CONCURRENTLY требует не быть внутри транзакции
        with op.get_context().autocommit_block():
            op.execute(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_traffic_purchases_sub_expires '
                'ON traffic_purchases (subscription_id, expires_at)'
            )
    else:
        op.create_index(
            'ix_traffic_purchases_sub_expires',
            'traffic_purchases',
            ['subscription_id', 'expires_at'],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_traffic_purchases_sub_expires')
    else:
        op.drop_index('ix_traffic_purchases_sub_expires', table_name='traffic_purchases')

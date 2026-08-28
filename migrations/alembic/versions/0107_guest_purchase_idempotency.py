"""guest_purchases: idempotency_key

Revision ID: 0107
Revises: 0106
Create Date: 2026-08-22

Добавляет ``idempotency_key`` в ``guest_purchases`` с уникальным индексом
``ux_guest_purchases_idempotency_key``. Позволяет гарантировать финансовую
идемпотентность покупок подарков с баланса на уровне базы данных.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0107'
down_revision: Union[str, None] = '0106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'idempotency_key' not in existing_cols:
        op.add_column(
            'guest_purchases',
            sa.Column('idempotency_key', sa.String(length=64), nullable=True),
        )

    existing_indexes = {idx['name'] for idx in inspector.get_indexes('guest_purchases')}
    if 'ux_guest_purchases_idempotency_key' not in existing_indexes:
        op.create_index(
            'ux_guest_purchases_idempotency_key',
            'guest_purchases',
            ['idempotency_key'],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('guest_purchases')}
    if 'ux_guest_purchases_idempotency_key' in existing_indexes:
        op.drop_index('ux_guest_purchases_idempotency_key', table_name='guest_purchases')

    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'idempotency_key' in existing_cols:
        op.drop_column('guest_purchases', 'idempotency_key')

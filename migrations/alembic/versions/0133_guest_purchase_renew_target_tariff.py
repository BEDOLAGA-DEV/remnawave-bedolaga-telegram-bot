"""guest_purchases: renew_target_tariff_id

Revision ID: 0133
Revises: 0132

Добавляет ``renew_target_tariff_id`` в ``guest_purchases``: тариф, на который нужно
перевести подписку после оплаты по публичной веб-ссылке (апселл триала с мёртвого
тарифа на платный). Колонка nullable, для обычного продления на текущем тарифе
остаётся NULL. Миграция идемпотентна — повторный прогон ничего не ломает.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0133'
down_revision: Union[str, None] = '0132'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'renew_target_tariff_id' not in existing_cols:
        op.add_column(
            'guest_purchases',
            sa.Column(
                'renew_target_tariff_id',
                sa.Integer(),
                sa.ForeignKey('tariffs.id', name='fk_guest_purchases_renew_target_tariff_id'),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'renew_target_tariff_id' in existing_cols:
        op.drop_column('guest_purchases', 'renew_target_tariff_id')

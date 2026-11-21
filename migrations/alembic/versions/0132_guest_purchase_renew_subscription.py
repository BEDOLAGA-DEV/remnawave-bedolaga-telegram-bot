"""guest_purchases: renew_subscription_id

Revision ID: 0132
Revises: 0131

Добавляет ``renew_subscription_id`` в ``guest_purchases``: подписку, которую нужно
продлить после оплаты по публичной веб-ссылке (``source = 'renew'``). Колонка
nullable, для всех остальных покупок остаётся NULL. Миграция идемпотентна —
повторный прогон на уже изменённой схеме ничего не ломает.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0132'
down_revision: Union[str, None] = '0131'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'renew_subscription_id' not in existing_cols:
        op.add_column(
            'guest_purchases',
            sa.Column(
                'renew_subscription_id',
                sa.Integer(),
                sa.ForeignKey('subscriptions.id', name='fk_guest_purchases_renew_subscription_id'),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'guest_purchases' not in inspector.get_table_names():
        return
    existing_cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
    if 'renew_subscription_id' in existing_cols:
        op.drop_column('guest_purchases', 'renew_subscription_id')

"""Выбор пользователя: куда класть дни и какой бонус получать

Revision ID: 0110
Revises: 0109
Create Date: 2026-08-27

Награда днями приходит асинхронно, на чужом пополнении, и подписку для неё
подбирал сам бот. При нескольких подписках подбор угадывает: он берёт платную с
самым поздним сроком, а человек хотел продлить другую. Здесь появляется его
собственный выбор — ссылкой на конкретную подписку, а не номером тарифа: на один
тариф подписок может быть несколько.

Второе поле — что предпочитает получать, когда правило платит и деньгами, и
днями. NULL в обоих означает прежнее поведение: подбирает бот, выдаётся всё.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0110'
down_revision: Union[str, None] = '0109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Внешнего ключа на subscriptions здесь НЕТ намеренно: между users и
# subscriptions уже есть связь subscriptions.user_id -> users.id, и вторая делает
# join между этими таблицами неоднозначным — SQLAlchemy перестаёт его выводить и
# роняет половину запросов приложения. Ссылка мягкая: протухший выбор
# проверяется запросом при начислении и превращается в автоподбор.
_COLUMNS = (
    ('referral_days_subscription_id', sa.Column('referral_days_subscription_id', sa.Integer(), nullable=True)),
    ('referral_reward_preference', sa.Column('referral_reward_preference', sa.String(length=10), nullable=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'users' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('users')}
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column('users', column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'users' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('users')}
    for name, _column in _COLUMNS:
        if name in existing:
            op.drop_column('users', name)

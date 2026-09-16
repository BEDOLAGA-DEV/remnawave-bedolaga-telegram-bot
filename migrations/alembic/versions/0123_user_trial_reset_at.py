"""дата админского сброса триала

Revision ID: 0123
Revises: 0122
Create Date: 2026-09-16

Кнопка «Сбросить триал» в кабинете сносила подписки, но не трогала отметку
«человек когда-то платил» (``users.has_had_paid_subscription``), а триал
закрывают обе вещи. У всех, кто хоть раз платил, кнопка не делала ничего —
помогало только удаление аккаунта.

Снимать саму отметку нельзя: по ней считаются конверсия, выручка и выборки
кампаний — она про факт оплаты, а не про право на триал. Эта колонка хранит
дату сброса и перекрывает отметку ровно до того момента, пока у человека снова
не появится подписка.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0123'
down_revision: Union[str, None] = '0122'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('users', 'trial_reset_at'):
        op.add_column('users', sa.Column('trial_reset_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column('users', 'trial_reset_at'):
        op.drop_column('users', 'trial_reset_at')

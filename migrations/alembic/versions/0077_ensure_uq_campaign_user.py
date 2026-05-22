"""ensure uq_campaign_user UNIQUE constraint on advertising_campaign_registrations

UniqueConstraint('campaign_id', 'user_id', name='uq_campaign_user') объявлен в
ORM-модели AdvertisingCampaignRegistration, но в legacy-таблицах, созданных до
ввода Alembic (через старый universal_migration.py + make migrate-stamp), он
физически может отсутствовать. Без него record_campaign_registration сейчас
полагается на read-then-write проверку, которая разрешает дубли при гонке
двух параллельных /start. Это ломает паритет между числом сообщений
✅ РЕГИСТРАЦИЯ ПО РК в админ-чате и числом строк в БД.

Миграция:
1. Дедуплицирует строки оставляя минимальный id для каждой пары
   (campaign_id, user_id).
2. Создаёт UNIQUE constraint, если его ещё нет.

Revision ID: 0077
Revises: 0076
Create Date: 2026-05-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0077'
down_revision: Union[str, None] = '0076'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONSTRAINT_NAME = 'uq_campaign_user'
_TABLE_NAME = 'advertising_campaign_registrations'


def _constraint_exists(bind: sa.engine.Connection) -> bool:
    """Проверяет, существует ли UNIQUE constraint в текущей БД."""
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE_NAME):
        return False
    for constraint in inspector.get_unique_constraints(_TABLE_NAME):
        if constraint.get('name') == _CONSTRAINT_NAME:
            return True
        cols = tuple(constraint.get('column_names') or ())
        if cols == ('campaign_id', 'user_id'):
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    if _constraint_exists(bind):
        return

    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE_NAME):
        return

    # Дедупликация: оставляем строку с минимальным id для каждой пары.
    bind.execute(
        sa.text(
            """
            DELETE FROM advertising_campaign_registrations a
            USING advertising_campaign_registrations b
            WHERE a.campaign_id = b.campaign_id
              AND a.user_id = b.user_id
              AND a.id > b.id
            """
        )
    )

    op.create_unique_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        ['campaign_id', 'user_id'],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _constraint_exists(bind):
        return
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_='unique')

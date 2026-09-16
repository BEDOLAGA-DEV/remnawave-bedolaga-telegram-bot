"""признак открытой грейс-сессии на подписке

Revision ID: 0121
Revises: 0120
Create Date: 2026-09-15

Пока грейс открыт, в панели стоит его оверлей: ACTIVE до конца грейса, сквад
грейса, лимит «расход + квота». Импорт «панель — истина» защищался флагом
``grace_open``, который каждый вызывающий должен был передать сам; мониторинг
(проверка «продлили в панели?» перед гашением), вход в кабинет по почте и
ручная кнопка «из панели в бота» его не передавали. Мониторинг переносил оверлей
в бота, воркер грейса видел более позднюю дату и закрывал грейс как оплату —
человек оставался в скваде грейса с лимитом в гигабайт (баг 2026-09-15).

Колонка — признак на самой подписке. Её ведёт хранилище грейс-сессий в той же
транзакции, что и состояние сессии; импорт видит признак сам. Заполняется по уже
открытым сессиям.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0121'
down_revision: Union[str, None] = '0120'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('subscriptions', 'grace_session_open'):
        op.add_column(
            'subscriptions',
            sa.Column('grace_session_open', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )
    inspector = sa.inspect(op.get_bind())
    if 'grace_access_sessions' in inspector.get_table_names():
        op.execute(
            """
            UPDATE subscriptions SET grace_session_open = true
            WHERE id IN (
                SELECT subscription_id FROM grace_access_sessions
                WHERE state IN ('pending', 'active', 'restoring')
            )
            """
        )


def downgrade() -> None:
    if _has_column('subscriptions', 'grace_session_open'):
        op.drop_column('subscriptions', 'grace_session_open')

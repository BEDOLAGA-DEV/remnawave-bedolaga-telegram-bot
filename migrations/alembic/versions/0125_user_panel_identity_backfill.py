"""Аккаунт панели человеку из его подписок (после мультитарифа)

Revision ID: 0125
Revises: 0124
Create Date: 2026-09-18

Аккаунты, созданные в мультитарифе, записывались только у подписки
(``subscriptions.remnawave_id``), а ``users.remnawave_id`` оставался пустым. После
возврата оператора в одиночный режим кабинет показывал «0 устройств», а покупка
заводила человеку второй аккаунт. Теперь код пишет аккаунт и человеку; уже
созданные строки дописываются здесь — только там, где ответ однозначен: у
человека ровно один аккаунт по всем его подпискам и этот id не записан другому
человеку (колонка уникальна).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0125'
down_revision: Union[str, None] = '0124'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if not {'users', 'subscriptions'} <= tables:
        return
    op.execute(
        sa.text(
            """
            UPDATE users
            SET remnawave_id = (
                SELECT MIN(s.remnawave_id) FROM subscriptions s
                WHERE s.user_id = users.id AND s.remnawave_id IS NOT NULL
            )
            WHERE users.remnawave_id IS NULL
              AND (
                  SELECT count(DISTINCT s.remnawave_id) FROM subscriptions s
                  WHERE s.user_id = users.id AND s.remnawave_id IS NOT NULL
              ) = 1
              AND NOT EXISTS (
                  SELECT 1 FROM users u2
                  WHERE u2.remnawave_id = (
                      SELECT MIN(s.remnawave_id) FROM subscriptions s
                      WHERE s.user_id = users.id AND s.remnawave_id IS NOT NULL
                  )
              )
            """
        )
    )


def downgrade() -> None:
    # Данные не откатываются: снятая запись вернула бы «0 устройств» в одиночном режиме.
    pass

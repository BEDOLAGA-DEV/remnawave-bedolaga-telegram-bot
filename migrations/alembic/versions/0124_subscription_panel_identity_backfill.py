"""id аккаунта панели у подписок, перенесённых из панели

Revision ID: 0124
Revises: 0123
Create Date: 2026-09-16

Импорт из панели в одиночном режиме записывал id аккаунта панели только в
``users.remnawave_id``, а ``subscriptions.remnawave_id`` оставлял пустым. Свой
путь бота пишет оба. Мультитариф и экраны по выбранной подписке читают строго id
подписки, поэтому у перенесённых людей кабинет и бот показывали 0 устройств.

Импорт исправлен, а уже перенесённые строки привязываются здесь — только там,
где ответ однозначен: у человека ровно одна подписка, это не черновик
неоплаченного триала, и этот id панели не закреплён за другой подпиской
(колонка частично уникальна). Остальное привязывает синхронизация.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0124'
down_revision: Union[str, None] = '0123'
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
            UPDATE subscriptions
            SET remnawave_id = (SELECT u.remnawave_id FROM users u WHERE u.id = subscriptions.user_id)
            WHERE subscriptions.remnawave_id IS NULL
              AND NOT (subscriptions.status = 'pending' AND subscriptions.is_trial)
              AND EXISTS (
                  SELECT 1 FROM users u
                  WHERE u.id = subscriptions.user_id AND u.remnawave_id IS NOT NULL
              )
              AND (SELECT count(*) FROM subscriptions s2 WHERE s2.user_id = subscriptions.user_id) = 1
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s3
                  JOIN users u ON u.id = subscriptions.user_id
                  WHERE s3.remnawave_id = u.remnawave_id
              )
            """
        )
    )


def downgrade() -> None:
    # Данные не откатываются: снятая привязка вернула бы «0 устройств».
    pass

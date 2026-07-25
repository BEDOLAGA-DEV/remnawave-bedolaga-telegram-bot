"""partial unique index: one alive platega binding per subscription

Гонка конкурентного enable (двойной тап / два устройства): проверка
идемпотентности в create_platega_sbp_subscription читает активную запись ДО
вставки — два конкурентных запроса могли оба пройти проверку и создать две
живые привязки. Видимой для отмены остаётся только последняя
(get_active_platega_subscription_by_subscription отдаёт .first() по id desc),
старая продолжила бы списывать невидимо. Индекс делает вторую вставку
IntegrityError — сервис ловит её и возвращает существующую запись.

Перед созданием индекса дублирующиеся живые привязки схлопываются: остаётся
новейшая (max id), остальные помечаются CANCELLED локально (remote-отмену
дочистит reconciler-свип CANCELLED-записей по remote-статусу).

Revision ID: 0100
Revises: 0099
"""

from alembic import op
import sqlalchemy as sa

revision = '0100'
down_revision = '0099'
branch_labels = None
depends_on = None

_ALIVE = "('PENDING', 'ACTIVE', 'PAST_DUE')"


def upgrade() -> None:
    # Схлопнуть существующие дубли живых привязок: оставить новейшую.
    op.execute(
        sa.text(
            f"""
            UPDATE platega_subscriptions
            SET status = 'CANCELLED', updated_at = NOW()
            WHERE status IN {_ALIVE}
              AND id NOT IN (
                  SELECT MAX(id) FROM platega_subscriptions
                  WHERE status IN {_ALIVE}
                  GROUP BY subscription_id
              )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_platega_subscriptions_alive
            ON platega_subscriptions (subscription_id)
            WHERE status IN {_ALIVE}
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text('DROP INDEX IF EXISTS uq_platega_subscriptions_alive'))

"""Серверный инвариант живого семейства, PostgreSQL.

Revision ID: 0133
Revises: 0132

Не изменяет тарифы, семейства, стоимость, пользовательские записи или
Remnawave identity. Для PostgreSQL устанавливает сериализующие триггеры;
не означает готовность финансовых сценариев к включению multi-tariff.
"""

from alembic import op
from sqlalchemy import text

from app.database.tariff_family_schema import PG_FAMILY_DDL, PG_FAMILY_DROP_DDL


revision = '0133'
down_revision = '0132'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite (тестовая/legacy БД) не поддерживает PostgreSQL advisory locks.
    # Данный этап не включает в SQLite семейное ограничение.
    if op.get_bind().dialect.name != 'postgresql':
        return

    connection = op.get_bind()
    # Окно миграции должно блокировать конкурирующие записи; проверяем лишь
    # агрегат нарушений, не выводя user_id и данные подписок в логи.
    connection.execute(text('LOCK TABLE tariffs, subscriptions IN SHARE ROW EXCLUSIVE MODE'))
    conflicts = connection.scalar(
        text(
            "SELECT count(*) FROM ("
            "SELECT s.user_id, t.entitlement_family "
            "FROM subscriptions s JOIN tariffs t ON t.id = s.tariff_id "
            "WHERE t.entitlement_family IS NOT NULL "
            "AND s.status IN ('active', 'trial', 'limited') "
            "GROUP BY s.user_id, t.entitlement_family HAVING count(*) > 1"
            ") conflicting"
        )
    )
    if conflicts:
        raise RuntimeError(f'Миграция 0133: обнаружены конфликты live семейства: {conflicts}')

    # На свежей схеме create_all мог уже установить эти триггеры.
    for statement in PG_FAMILY_DROP_DDL:
        op.execute(statement)
    for statement in PG_FAMILY_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return

    for statement in PG_FAMILY_DROP_DDL:
        op.execute(statement)

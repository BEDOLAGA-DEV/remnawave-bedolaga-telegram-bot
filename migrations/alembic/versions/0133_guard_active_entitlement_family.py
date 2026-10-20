"""Серверный инвариант живого семейства, PostgreSQL.

Revision ID: 0133
Revises: 0132

Не изменяет тарифы, семейства, стоимость, пользовательские записи или
Remnawave identity. Для PostgreSQL устанавливает сериализующие триггеры;
не означает готовность финансовых сценариев к включению multi-tariff.
"""

from alembic import op

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

    for statement in PG_FAMILY_DDL:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return

    for statement in PG_FAMILY_DROP_DDL:
        op.execute(statement)

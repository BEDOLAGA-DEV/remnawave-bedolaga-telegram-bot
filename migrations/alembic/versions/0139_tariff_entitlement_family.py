"""Необязательная коммерческая группа тарифов (пока только метаданные).

Revision ID: 0139
Revises: 0138

NULL сохраняет существующее поведение всех инсталляций. Миграция не
назначает legacy-тарифам группу и не блокирует создание подписок.
"""

import sqlalchemy as sa
from alembic import op


revision = '0139'
down_revision = '0138'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(sa.Column('entitlement_family', sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Блокировка исключает запись между проверкой и удалением колонки.
        bind.execute(sa.text('LOCK TABLE tariffs IN ACCESS EXCLUSIVE MODE'))
    if bind.execute(sa.text('SELECT 1 FROM tariffs WHERE entitlement_family IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Нельзя удалить назначенные семейства тарифов при откате')
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('entitlement_family')

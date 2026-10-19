"""Необязательная коммерческая группа тарифов (пока только метаданные).

Revision ID: 0132
Revises: 0131

NULL сохраняет существующее поведение всех инсталляций. Миграция не
назначает legacy-тарифам группу и не блокирует создание подписок.
"""

import sqlalchemy as sa
from alembic import op


revision = '0132'
down_revision = '0131'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(sa.Column('entitlement_family', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('entitlement_family')

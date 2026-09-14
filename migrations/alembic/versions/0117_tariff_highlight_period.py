"""выделение периода тарифа как самого выгодного

Revision ID: 0117
Revises: 0116
Create Date: 2026-09-08

Оператор выбирает один из периодов тарифа, и он показывается выделенным —
в боте отметкой в кнопке, в кабинете рамкой с подписью. Хранится числом дней,
а не индексом: набор периодов правят, и индекс после правки указывал бы на
другой период.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0117'
down_revision: Union[str, None] = '0116'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if _has_column('tariffs', 'highlight_period_days'):
        return
    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(sa.Column('highlight_period_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('highlight_period_days')

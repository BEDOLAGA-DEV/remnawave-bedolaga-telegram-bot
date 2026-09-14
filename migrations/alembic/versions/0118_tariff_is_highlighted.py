"""выделение самого тарифа в списке

Revision ID: 0118
Revises: 0117
Create Date: 2026-09-08

Периоды внутри тарифа выделять уже можно (0117), но список тарифов — отдельный
экран: сначала выбирают тариф, потом период. Отметка тарифа живёт своим флагом.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0118'
down_revision: Union[str, None] = '0117'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if _has_column('tariffs', 'is_highlighted'):
        return
    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(
            sa.Column('is_highlighted', sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('is_highlighted')

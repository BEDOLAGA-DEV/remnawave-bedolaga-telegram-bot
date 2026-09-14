"""тег панели и дни триала у тарифа

Revision ID: 0119
Revises: 0118
Create Date: 2026-09-10

panel_tag — свой тег панельного пользователя для тарифа (побеждает общие
TRIAL/PAID из настроек): по нему в панели видно, на каком тарифе человек.

trial_duration_days — дни триала на конкретном тарифе. Колонки не было:
телеграм-редактор писал значение в атрибут объекта, покупка читала его через
getattr и всегда получала пусто.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0119'
down_revision: Union[str, None] = '0118'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    missing = [
        column
        for column in (
            sa.Column('panel_tag', sa.String(length=16), nullable=True),
            sa.Column('trial_duration_days', sa.Integer(), nullable=True),
        )
        if not _has_column('tariffs', column.name)
    ]
    if not missing:
        return
    with op.batch_alter_table('tariffs') as batch:
        for column in missing:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('trial_duration_days')
        batch.drop_column('panel_tag')

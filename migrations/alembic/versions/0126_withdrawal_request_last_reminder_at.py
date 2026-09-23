"""отметка последнего напоминания о заявке на вывод

Revision ID: 0126
Revises: 0125
Create Date: 2026-09-19

О заявке на вывод реферального баланса админы получали одно уведомление — в
момент создания. У тикетов поддержки есть SLA-напоминалка, у заявок не было:
заявка, которую никто не открыл, терялась в потоке. Теперь мониторинг напоминает
о заявках в статусе pending с тем же механизмом, что у тикетов; эта колонка —
отметка последнего напоминания, по ней считается кулдаун повторов.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0126'
down_revision: Union[str, None] = '0125'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('withdrawal_requests', 'last_reminder_at'):
        op.add_column('withdrawal_requests', sa.Column('last_reminder_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column('withdrawal_requests', 'last_reminder_at'):
        op.drop_column('withdrawal_requests', 'last_reminder_at')

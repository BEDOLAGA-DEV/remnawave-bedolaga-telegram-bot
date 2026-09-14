"""хвост грейса: дата, оставленная грейс-доступом в панели

Revision ID: 0120
Revises: 0119
Create Date: 2026-09-14

После окончания grace-доступа в панели остаётся дата конца грейса: прошедшую
дату PATCH /api/users не принимает, и вернуть настоящую нельзя. Импорт
«панель — истина» переносил её в бота как новую дату окончания, воркер грейса
видел «только что истекла» и выдавал грейс заново — бесконечно, раз в срок
грейса. Колонка хранит эту дату, и импорт, увидев в панели ровно её, дату и
статус подписки не трогает.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0120'
down_revision: Union[str, None] = '0119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('subscriptions', 'grace_tail_expire_at'):
        op.add_column(
            'subscriptions',
            sa.Column('grace_tail_expire_at', sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column('subscriptions', 'grace_tail_expire_at'):
        op.drop_column('subscriptions', 'grace_tail_expire_at')

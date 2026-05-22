"""add revocation_source to user_roles

Позволяет отличить env-revoke (bootstrap снял роль потому что юзер выпал из
ADMIN_IDS/ADMIN_EMAILS) от ui-revoke (senior-админ вручную отозвал роль через
кабинет). Без этого поля `_assign_if_missing` на каждом рестарте бота
безусловно реактивирует все revoked-роли — игнорируя ручной отзыв через UI.

Существующие `is_active=False` строки остаются с `revocation_source=NULL`.
В новом коде NULL трактуется как «legacy / env-style» и реактивируется как
раньше — backward-compatible для всех уже накопленных revokes на проде.

Revision ID: 0078
Revises: 0077
Create Date: 2026-05-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0078'
down_revision: Union[str, None] = '0077'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = 'user_roles'
_COLUMN = 'revocation_source'


def _column_exists(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(col['name'] == _COLUMN for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    if _column_exists(bind):
        return

    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind):
        return
    op.drop_column(_TABLE, _COLUMN)

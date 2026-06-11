"""yandex_client_id_map: add yclid column for offline-conversion uploads

Captures the Yandex click id (`?yclid=...` from the landing URL) so that on
every purchase we can upload an offline conversion to the Metrika Offline
Conversions API keyed by yclid. Unlike the ClientID-based Measurement
Protocol (session-bound, only attributes the first purchase), the yclid is
session-independent → repeat purchases by the same user attribute correctly.

Revision ID: 0090
Revises: 0089
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0090'
down_revision: Union[str, None] = '0089'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = 'yandex_client_id_map'
_COLUMN = 'yclid'


def _column_exists(bind: sa.engine.Connection) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(col['name'] == _COLUMN for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    if not _column_exists(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind):
        op.drop_column(_TABLE, _COLUMN)

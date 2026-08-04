"""add account deletion active request uniqueness

Revision ID: 0088
Revises: 0087
Create Date: 2026-05-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0088'
down_revision: Union[str, None] = '0087'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'account_deletion_requests'
_INDEX = 'uq_account_deletion_requests_user_active'
_CLAIM_TOKEN_COLUMN = 'claim_token'


def _table_exists(bind: sa.engine.Connection) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def _index_exists(bind: sa.engine.Connection, index_name: str) -> bool:
    if not _table_exists(bind):
        return False
    return any(index['name'] == index_name for index in sa.inspect(bind).get_indexes(_TABLE))


def _column_exists(bind: sa.engine.Connection, column_name: str) -> bool:
    if not _table_exists(bind):
        return False
    return any(column['name'] == column_name for column in sa.inspect(bind).get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return

    if not _column_exists(bind, _CLAIM_TOKEN_COLUMN):
        op.add_column(_TABLE, sa.Column(_CLAIM_TOKEN_COLUMN, sa.String(length=64), nullable=True))

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    row_number() OVER (
                        PARTITION BY user_id
                        ORDER BY
                            CASE WHEN status = 'processing' THEN 0 ELSE 1 END,
                            created_at DESC NULLS LAST,
                            id DESC
                    ) AS rn
                FROM account_deletion_requests
                WHERE user_id IS NOT NULL
                  AND status IN ('pending', 'processing')
            )
            UPDATE account_deletion_requests AS request
            SET
                status = 'failed',
                last_error = 'Superseded by another active account deletion request before unique index creation',
                updated_at = NOW(),
                next_retry_at = NOW(),
                claim_token = NULL
            FROM ranked
            WHERE request.id = ranked.id
              AND ranked.rn > 1
            """
        )
    )

    if _index_exists(bind, _INDEX):
        return

    op.create_index(
        _INDEX,
        _TABLE,
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL AND status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _column_exists(bind, _CLAIM_TOKEN_COLUMN):
        op.drop_column(_TABLE, _CLAIM_TOKEN_COLUMN)

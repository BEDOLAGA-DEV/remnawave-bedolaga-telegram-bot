"""alter notification_settings from json to jsonb

Revision ID: 0057
Revises: 0056
Create Date: 2026-04-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0057'
down_revision: Union[str, None] = '0056'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_column_type(table: str, column: str) -> str | None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {'table': table, 'column': column},
    )
    row = result.fetchone()
    return row[0] if row else None


def upgrade() -> None:
    col_type = _get_column_type('users', 'notification_settings')
    if col_type and col_type != 'jsonb':
        op.execute(
            sa.text(
                "ALTER TABLE users "
                "ALTER COLUMN notification_settings TYPE jsonb "
                "USING notification_settings::jsonb"
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE users "
            "ALTER COLUMN notification_settings TYPE json "
            "USING notification_settings::json"
        )
    )

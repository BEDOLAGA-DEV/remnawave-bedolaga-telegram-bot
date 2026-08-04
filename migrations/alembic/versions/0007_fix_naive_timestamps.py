"""fix all remaining naive timestamp columns to timestamptz

Revision ID: 0007
Revises: 0006
Create Date: 2026-02-23

The old universal_migration.py created some tables with `timestamp` (naive)
columns and had a catch-all migration that converted ALL naive timestamp
columns to `timestamptz` on every startup. When universal_migration.py was
replaced with Alembic, that catch-all migration stopped running.

Databases where `email_templates` (and potentially other tables) were created
by universal_migration.py before the catch-all ran still have naive columns.
The code uses `datetime.now(UTC)` (timezone-aware), causing asyncpg to raise:
  "can't subtract offset-naive and offset-aware datetimes"

This migration finds and converts ALL remaining naive timestamp columns
in public schema to timestamptz, assuming UTC for existing data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find all naive timestamp columns in public schema
    result = conn.execute(
        sa.text("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND data_type = 'timestamp without time zone'
            ORDER BY table_name, column_name
        """)
    )
    columns = result.fetchall()

    if not columns:
        return

    # Set timezone context for the conversion
    conn.execute(sa.text("SET LOCAL timezone = 'UTC'"))

    for table_name, column_name in columns:
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" '
                f'ALTER COLUMN "{column_name}" TYPE TIMESTAMPTZ '
                f'USING "{column_name}" AT TIME ZONE \'UTC\''
            )
        )


def downgrade() -> None:
    # No-op: converting back to naive timestamps would lose timezone info
    # and re-introduce the original bug.
    pass

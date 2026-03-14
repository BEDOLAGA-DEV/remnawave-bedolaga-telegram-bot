"""add CHECK constraints to landing_pages discount columns

Ensures discount_percent is in range 1-99 and discount_starts_at < discount_ends_at
at the database level as defense in depth.

Revision ID: 0028
Revises: 0027
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: Union[str, None] = '0027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_check_constraint(table: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND constraint_name = :name AND constraint_type = 'CHECK'"
        ),
        {'table': table, 'name': constraint_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if not _has_check_constraint('landing_pages', 'chk_landing_discount_percent_range'):
        op.create_check_constraint(
            'chk_landing_discount_percent_range',
            'landing_pages',
            'discount_percent IS NULL OR (discount_percent >= 1 AND discount_percent <= 99)',
        )
    if not _has_check_constraint('landing_pages', 'chk_landing_discount_dates_order'):
        op.create_check_constraint(
            'chk_landing_discount_dates_order',
            'landing_pages',
            'discount_starts_at IS NULL OR discount_ends_at IS NULL OR discount_starts_at < discount_ends_at',
        )


def downgrade() -> None:
    op.drop_constraint('chk_landing_discount_dates_order', 'landing_pages', type_='check')
    op.drop_constraint('chk_landing_discount_percent_range', 'landing_pages', type_='check')

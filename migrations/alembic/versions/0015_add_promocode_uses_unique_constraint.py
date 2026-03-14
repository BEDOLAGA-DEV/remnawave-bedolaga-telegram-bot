"""add unique constraint on promocode_uses(user_id, promocode_id)

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-06

Prevents race condition where concurrent requests could create
duplicate PromoCodeUse records for the same user+promocode.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(table: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table "
            "AND constraint_name = :name "
            "AND constraint_type = 'UNIQUE'"
        ),
        {'table': table, 'name': constraint_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # Deduplicate any existing rows before adding constraint
    op.execute("""
        DELETE FROM promocode_uses
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM promocode_uses
            GROUP BY user_id, promocode_id
        )
    """)

    if not _constraint_exists('promocode_uses', 'uq_promocode_uses_user_promo'):
        op.create_unique_constraint(
            'uq_promocode_uses_user_promo',
            'promocode_uses',
            ['user_id', 'promocode_id'],
        )


def downgrade() -> None:
    op.drop_constraint('uq_promocode_uses_user_promo', 'promocode_uses', type_='unique')

"""make promo_group_id nullable in users table

The SQLAlchemy model declares promo_group_id as nullable=True but the
actual PostgreSQL column has a NOT NULL constraint, causing failures
when creating guest users without a promo group.

Revision ID: 0023
Revises: 0022
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: Union[str, None] = '0022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'promo_group_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # WARNING: Will fail if any rows have promo_group_id=NULL.
    # Backfill required before downgrading.
    op.alter_column('users', 'promo_group_id', existing_type=sa.Integer(), nullable=False)

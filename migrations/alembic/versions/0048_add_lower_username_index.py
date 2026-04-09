"""add functional index on lower(username) for phantom user lookup

Revision ID: 0048
Revises: 0047
Create Date: 2026-03-23

The find_phantom_user_by_username query uses func.lower(User.username)
which cannot use a regular B-tree index on username. This adds a
functional index to avoid sequential scans on the users table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0048'
down_revision: str | None = '0047'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_username_lower '
                'ON users (lower(username))'
            )
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text('DROP INDEX CONCURRENTLY IF EXISTS ix_users_username_lower'))

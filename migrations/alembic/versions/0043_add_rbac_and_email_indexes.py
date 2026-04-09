"""add missing indexes for RBAC foreign keys and lower(email) expression

Revision ID: 0043
Revises: 0042
Create Date: 2026-03-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0043'
down_revision: Union[str, None] = '0042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_roles_role_id '
                'ON user_roles (role_id)'
            )
        )
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_access_policies_role_id '
                'ON access_policies (role_id)'
            )
        )
        op.execute(
            sa.text(
                'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_email_lower '
                'ON users (lower(email))'
            )
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text('DROP INDEX CONCURRENTLY IF EXISTS ix_users_email_lower'))
        op.execute(sa.text('DROP INDEX CONCURRENTLY IF EXISTS ix_access_policies_role_id'))
        op.execute(sa.text('DROP INDEX CONCURRENTLY IF EXISTS ix_user_roles_role_id'))

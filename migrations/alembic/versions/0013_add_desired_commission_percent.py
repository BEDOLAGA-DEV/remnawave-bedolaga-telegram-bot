"""add desired_commission_percent to partner_applications

Revision ID: 0013
Revises: 0012
Create Date: 2026-02-27

Adds column for partners to specify their desired commission percentage
when applying. Fresh databases created via 0001 (create_all) may already
have it, so we use checkfirst logic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('partner_applications', 'desired_commission_percent'):
        op.add_column(
            'partner_applications',
            sa.Column('desired_commission_percent', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('partner_applications', 'desired_commission_percent')

"""Drop legacy prize_days column from contest_templates

Revision ID: 0031
Revises: 0030
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0031'
down_revision: Union[str, None] = '0030'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return insp.has_table(table_name)


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if _table_exists('contest_templates') and _has_column('contest_templates', 'prize_days'):
        op.drop_column('contest_templates', 'prize_days')


def downgrade() -> None:
    if _table_exists('contest_templates') and not _has_column('contest_templates', 'prize_days'):
        op.add_column(
            'contest_templates',
            sa.Column('prize_days', sa.Integer(), nullable=True),
        )

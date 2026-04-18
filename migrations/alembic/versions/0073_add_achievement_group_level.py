"""add group_name and level to achievement_templates

Revision ID: 0073
Revises: 0072
Create Date: 2026-04-13

Multi-level achievements: templates with the same group_name form
a chain of levels (1 -> 2 -> 3). Level N+1 unlocks only after N.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0073'
down_revision: Union[str, None] = '0072'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'achievement_templates' not in inspector.get_table_names():
        return

    existing = {c['name'] for c in inspector.get_columns('achievement_templates')}

    if 'group_name' not in existing:
        op.add_column(
            'achievement_templates',
            sa.Column('group_name', sa.String(100), nullable=True),
        )

    if 'level' not in existing:
        op.add_column(
            'achievement_templates',
            sa.Column('level', sa.Integer(), server_default='1', nullable=False),
        )


def downgrade() -> None:
    op.drop_column('achievement_templates', 'level')
    op.drop_column('achievement_templates', 'group_name')

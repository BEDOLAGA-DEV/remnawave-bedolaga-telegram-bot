"""add is_hidden and hint to achievement_templates

Revision ID: 0065
Revises: 0064
Create Date: 2026-04-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0065'
down_revision: Union[str, None] = '0064'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'achievement_templates' not in inspector.get_table_names():
        return

    existing = {c['name'] for c in inspector.get_columns('achievement_templates')}

    if 'is_hidden' not in existing:
        op.add_column(
            'achievement_templates',
            sa.Column('is_hidden', sa.Boolean(), server_default='false', nullable=False),
        )

    if 'hint' not in existing:
        op.add_column(
            'achievement_templates',
            sa.Column('hint', sa.String(500), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('achievement_templates', 'hint')
    op.drop_column('achievement_templates', 'is_hidden')

"""add landing_pages analytics columns + sticky_pay_button

Revision ID: 0062
Revises: 0061
Create Date: 2026-04-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0062'
down_revision: Union[str, None] = '0061'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COLUMNS = (
    (
        'sticky_pay_button',
        sa.Column('sticky_pay_button', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    ),
    (
        'analytics_view_enabled',
        sa.Column('analytics_view_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    ),
    ('analytics_view_goal', sa.Column('analytics_view_goal', sa.String(64), nullable=True)),
    (
        'analytics_click_enabled',
        sa.Column('analytics_click_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    ),
    ('analytics_click_goal', sa.Column('analytics_click_goal', sa.String(64), nullable=True)),
)


def upgrade() -> None:
    conn = op.get_bind()
    for col_name, col_def in NEW_COLUMNS:
        result = conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
                "WHERE table_name = 'landing_pages' AND column_name = :col)"
            ),
            {'col': col_name},
        )
        if not result.scalar():
            op.add_column('landing_pages', col_def)


def downgrade() -> None:
    conn = op.get_bind()
    for col_name, _ in reversed(NEW_COLUMNS):
        result = conn.execute(
            sa.text(
                'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
                "WHERE table_name = 'landing_pages' AND column_name = :col)"
            ),
            {'col': col_name},
        )
        if result.scalar():
            op.drop_column('landing_pages', col_name)

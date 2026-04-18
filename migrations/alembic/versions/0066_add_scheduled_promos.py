"""add scheduled_promos table

Time-limited promotional discounts for tariffs.

Revision ID: 0066
Revises: 0065
Create Date: 2026-04-08

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0066'
down_revision: Union[str, None] = '0065'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'scheduled_promos' in inspector.get_table_names():
        return

    op.create_table(
        'scheduled_promos',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('discount_percent', sa.Integer(), nullable=False),
        sa.Column('tariff_ids', sa.JSON(), server_default='[]'),
        sa.Column('promo_text', sa.Text(), nullable=True),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column(
            'created_by',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_scheduled_promos_active_dates', 'scheduled_promos', ['is_active', 'start_at', 'end_at'])


def downgrade() -> None:
    op.drop_index('ix_scheduled_promos_active_dates', table_name='scheduled_promos')
    op.drop_table('scheduled_promos')

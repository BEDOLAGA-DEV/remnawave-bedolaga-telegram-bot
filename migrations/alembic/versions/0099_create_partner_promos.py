"""create partner_promos table

Revision ID: 0099
Revises: 0098
Create Date: 2026-05-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'partner_promos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('description', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('image_url', sa.String(2048), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('partner_promos')

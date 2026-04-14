"""add personal_data_consents table

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0058'
down_revision: Union[str, None] = '0057'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'personal_data_consents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('language', sa.String(10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('language'),
    )
    op.create_index('ix_personal_data_consents_id', 'personal_data_consents', ['id'])


def downgrade() -> None:
    op.drop_index('ix_personal_data_consents_id', table_name='personal_data_consents')
    op.drop_table('personal_data_consents')

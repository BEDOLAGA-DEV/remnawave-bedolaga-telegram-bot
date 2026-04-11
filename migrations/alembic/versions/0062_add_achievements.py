"""add achievements

Create achievement_templates and user_achievements tables.

Revision ID: 0062
Revises: 0061
Create Date: 2026-04-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0062'
down_revision: Union[str, None] = '0061'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    if 'achievement_templates' not in existing_tables:
        op.create_table(
            'achievement_templates',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('emoji', sa.String(10), server_default='\U0001f3c6'),
            sa.Column('condition_type', sa.String(50), nullable=False),
            sa.Column('condition_value', sa.Integer(), nullable=False),
            sa.Column('reward_type', sa.String(50), nullable=False),
            sa.Column('reward_value', sa.Integer(), server_default='0'),
            sa.Column('reward_duration_days', sa.Integer(), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true'),
            sa.Column('display_order', sa.Integer(), server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if 'user_achievements' not in existing_tables:
        op.create_table(
            'user_achievements',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column(
                'template_id',
                sa.Integer(),
                sa.ForeignKey('achievement_templates.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('unlocked_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('reward_claimed', sa.Boolean(), server_default='false'),
            sa.UniqueConstraint('user_id', 'template_id', name='uq_user_achievement'),
        )
        op.create_index('ix_user_achievements_user_id', 'user_achievements', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_achievements_user_id', table_name='user_achievements')
    op.drop_table('user_achievements')
    op.drop_table('achievement_templates')

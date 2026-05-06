"""create tasks system: tasks, user_task_progress, task_partner_channels + tariffs.bonus_days_per_purchase

Revision ID: 0075
Revises: 0074
Create Date: 2026-05-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0075'
down_revision: Union[str, None] = '0074'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Tariff: bonus_days_per_purchase
    op.add_column(
        'tariffs',
        sa.Column('bonus_days_per_purchase', sa.Integer(), nullable=False, server_default='0'),
    )

    # 2. Партнёрские каналы для заданий
    op.create_table(
        'task_partner_channels',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('channel_id', sa.String(100), unique=True, nullable=False, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('channel_link', sa.String(500), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 3. Шаблоны заданий
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('title', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('description', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('icon', sa.String(50), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('task_type', sa.String(32), nullable=False, index=True),
        sa.Column('target_value', sa.BigInteger(), nullable=False, server_default='1'),
        sa.Column('target_meta', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('reward_type', sa.String(32), nullable=False),
        sa.Column('reward_value', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('reward_meta', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('allow_user_choice', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('user_audience', sa.String(16), nullable=False, server_default='both'),
        sa.Column(
            'promo_group_id',
            sa.Integer(),
            sa.ForeignKey('promo_groups.id', ondelete='SET NULL'),
            nullable=True,
            index=True,
        ),
        sa.Column(
            'parent_task_id',
            sa.Integer(),
            sa.ForeignKey('tasks.id', ondelete='SET NULL'),
            nullable=True,
            index=True,
        ),
        sa.Column('level', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 4. Прогресс пользователей
    op.create_table(
        'user_task_progress',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True
        ),
        sa.Column(
            'task_id', sa.Integer(), sa.ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False, index=True
        ),
        sa.Column('current_value', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('period_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('baseline_value', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reward_granted_meta', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'task_id', name='uq_user_task'),
    )


def downgrade() -> None:
    op.drop_table('user_task_progress')
    op.drop_table('tasks')
    op.drop_table('task_partner_channels')
    op.drop_column('tariffs', 'bonus_days_per_purchase')

"""create referral milestones tables

Revision ID: 0100
Revises: 0099
Create Date: 2026-05-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0100'
down_revision: Union[str, None] = '0099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'referral_milestones',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('threshold', sa.Integer(), nullable=False),
        sa.Column('reward_type', sa.String(20), nullable=False),
        sa.Column('reward_value', sa.Integer(), nullable=False),
        sa.Column('title', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('threshold', name='uq_referral_milestone_threshold'),
    )
    op.create_table(
        'user_referral_milestone_claims',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('milestone_id', sa.Integer(), sa.ForeignKey('referral_milestones.id', ondelete='CASCADE'), nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'milestone_id', name='uq_user_milestone_claim'),
    )


def downgrade() -> None:
    op.drop_table('user_referral_milestone_claims')
    op.drop_table('referral_milestones')

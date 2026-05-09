"""add bio_reward_analytics_snapshot table

Cache table for bio-reward analytics (conversion cohorts + viral coefficient).

Revision ID: 0079
Revises: 0078
Create Date: 2026-05-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0079'
down_revision: str | None = '0078'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'bio_reward_analytics_snapshot',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('snapshot_type', sa.String(40), nullable=False),
        sa.Column('bucket_key', sa.String(40), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            'snapshot_type', 'bucket_key', name='uq_bio_reward_analytics_type_bucket'
        ),
    )
    op.create_index(
        'ix_bio_reward_analytics_type_bucket',
        'bio_reward_analytics_snapshot',
        ['snapshot_type', 'bucket_key'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_bio_reward_analytics_type_bucket',
        table_name='bio_reward_analytics_snapshot',
    )
    op.drop_table('bio_reward_analytics_snapshot')

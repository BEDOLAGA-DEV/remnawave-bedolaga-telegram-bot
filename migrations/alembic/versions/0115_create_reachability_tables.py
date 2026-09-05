"""create reachability tables (bschekbot integration)

Revision ID: 0115
Revises: 0114
Create Date: 2026-09-05

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0115'
down_revision: Union[str, None] = '0114'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reachability_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('phase', sa.String(32), nullable=True),
        sa.Column('trigger', sa.String(16), nullable=False, server_default='manual'),
        sa.Column('started_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('idempotency_key', sa.String(64), nullable=False, unique=True),
        sa.Column('external_id', sa.Integer(), nullable=True),
        sa.Column('last_request_id', sa.String(64), nullable=True),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('targets', sa.JSON(), nullable=False),
        sa.Column('units_requested', sa.JSON(), nullable=True),
        sa.Column('units_resolved', sa.JSON(), nullable=True),
        sa.Column('units_effective', sa.JSON(), nullable=True),
        sa.Column('skipped', sa.JSON(), nullable=True),
        sa.Column('dpi', sa.String(8), nullable=False, server_default='on'),
        sa.Column('estimated_kopeks', sa.Integer(), nullable=True),
        sa.Column('estimate_is_exact', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('cost_kopeks', sa.Integer(), nullable=True),
        sa.Column('refunded_kopeks', sa.Integer(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_reachability_jobs_id', 'reachability_jobs', ['id'])
    op.create_index('ix_reachability_jobs_status', 'reachability_jobs', ['status'])
    op.create_index('ix_reachability_jobs_started_by_user_id', 'reachability_jobs', ['started_by_user_id'])
    op.create_index('ix_reachability_jobs_external_id', 'reachability_jobs', ['external_id'])
    op.create_index('ix_reachability_jobs_kind_created', 'reachability_jobs', ['kind', 'created_at'])

    op.create_table(
        'reachability_legs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('reachability_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('target_key', sa.String(255), nullable=False),
        sa.Column('target_kind', sa.String(32), nullable=True),
        sa.Column('target_ref', sa.String(255), nullable=True),
        sa.Column('op_key', sa.String(64), nullable=False),
        sa.Column('operator', sa.String(32), nullable=True),
        sa.Column('region', sa.String(32), nullable=True),
        sa.Column('dpi', sa.String(8), nullable=True),
        sa.Column('verdict', sa.String(16), nullable=False),
        sa.Column('matches_expectation', sa.Boolean(), nullable=True),
        sa.Column('raw', sa.JSON(), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_reachability_legs_id', 'reachability_legs', ['id'])
    op.create_index('ix_reachability_legs_job_id', 'reachability_legs', ['job_id'])
    op.create_index(
        'ix_reachability_legs_target_unit_time', 'reachability_legs', ['target_key', 'op_key', 'checked_at']
    )

    op.create_table(
        'reachability_target_prefs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('target_kind', sa.String(32), nullable=False),
        sa.Column('target_ref', sa.String(255), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False, server_default='unknown'),
        sa.Column('excluded', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('updated_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('target_kind', 'target_ref', name='uq_reachability_target_prefs_target'),
    )
    op.create_index('ix_reachability_target_prefs_id', 'reachability_target_prefs', ['id'])


def downgrade() -> None:
    op.drop_table('reachability_target_prefs')
    op.drop_table('reachability_legs')
    op.drop_table('reachability_jobs')

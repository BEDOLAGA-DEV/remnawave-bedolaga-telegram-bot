"""reachability batches: проверка многих серверов одной кнопкой

Revision ID: 0116
Revises: 0115
Create Date: 2026-09-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0116'
down_revision: Union[str, None] = '0115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reachability_batches',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('phase', sa.String(32), nullable=True),
        sa.Column('started_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('scope', sa.JSON(), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('total_targets', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_kopeks', sa.Integer(), nullable=True),
        sa.Column('cost_kopeks', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_reachability_batches_id', 'reachability_batches', ['id'])
    op.create_index('ix_reachability_batches_status', 'reachability_batches', ['status'])
    op.create_index('ix_reachability_batches_started_by_user_id', 'reachability_batches', ['started_by_user_id'])

    with op.batch_alter_table('reachability_jobs') as batch_op:
        batch_op.add_column(sa.Column('batch_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_reachability_jobs_batch_id', 'reachability_batches', ['batch_id'], ['id'], ondelete='SET NULL'
        )
        batch_op.create_index('ix_reachability_jobs_batch_id', ['batch_id'])


def downgrade() -> None:
    with op.batch_alter_table('reachability_jobs') as batch_op:
        batch_op.drop_index('ix_reachability_jobs_batch_id')
        batch_op.drop_constraint('fk_reachability_jobs_batch_id', type_='foreignkey')
        batch_op.drop_column('batch_id')
    op.drop_table('reachability_batches')

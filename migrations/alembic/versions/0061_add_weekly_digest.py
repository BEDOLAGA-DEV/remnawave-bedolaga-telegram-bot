"""add weekly digest

Add digest_enabled to users and create weekly_digest_records table.

Revision ID: 0061
Revises: 0060
Create Date: 2026-04-09

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0061'
down_revision: Union[str, None] = '0060'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    user_cols = {c['name'] for c in inspector.get_columns('users')}
    if 'digest_enabled' not in user_cols:
        op.add_column(
            'users',
            sa.Column('digest_enabled', sa.Boolean(), server_default='true', nullable=False),
        )

    if 'weekly_digest_records' not in inspector.get_table_names():
        op.create_table(
            'weekly_digest_records',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('week_year', sa.String(10), nullable=False),
            sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('user_id', 'week_year', name='uq_digest_user_week'),
        )
        op.create_index('ix_weekly_digest_records_user_id', 'weekly_digest_records', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_weekly_digest_records_user_id', table_name='weekly_digest_records')
    op.drop_table('weekly_digest_records')
    op.drop_column('users', 'digest_enabled')

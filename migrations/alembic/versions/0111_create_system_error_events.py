"""create system_error_events table

Revision ID: 0111
Revises: 0110
Create Date: 2026-08-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0111'
down_revision: Union[str, None] = '0110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'system_error_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_uid', sa.String(32), unique=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('level', sa.String(16), nullable=False, server_default='error'),
        sa.Column('logger_name', sa.String(255), nullable=True),
        sa.Column('event', sa.Text(), nullable=False),
        sa.Column('error_type', sa.String(255), nullable=True),
        sa.Column('traceback', sa.Text(), nullable=True),
        sa.Column('context', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('dedup_hash', sa.String(32), nullable=True),
        sa.Column('delivery_status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('delivery_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivery_error', sa.Text(), nullable=True),
    )
    op.create_index('ix_system_error_events_event_uid', 'system_error_events', ['event_uid'], unique=True)
    op.create_index('ix_system_error_events_created_at', 'system_error_events', ['created_at'])
    op.create_index('ix_system_error_events_level', 'system_error_events', ['level'])
    op.create_index('ix_system_error_events_logger_name', 'system_error_events', ['logger_name'])
    op.create_index('ix_system_error_events_error_type', 'system_error_events', ['error_type'])
    op.create_index('ix_system_error_events_user_id', 'system_error_events', ['user_id'])
    op.create_index('ix_system_error_events_status_created', 'system_error_events', ['delivery_status', 'created_at'])
    op.create_index('ix_system_error_events_dedup_created', 'system_error_events', ['dedup_hash', 'created_at'])


def downgrade() -> None:
    op.drop_table('system_error_events')

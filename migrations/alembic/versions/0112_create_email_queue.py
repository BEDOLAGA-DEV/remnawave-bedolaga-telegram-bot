"""create email_queue table

Revision ID: 0112
Revises: 0111
Create Date: 2026-08-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0112'
down_revision: Union[str, None] = '0111'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'email_queue',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('to_email', sa.String(320), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('body_html', sa.Text(), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=True),
        sa.Column('unsubscribe_url', sa.Text(), nullable=True),
        sa.Column('attachments_json', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_email_queue_to_email', 'email_queue', ['to_email'])
    op.create_index('ix_email_queue_status', 'email_queue', ['status'])
    op.create_index('ix_email_queue_created_at', 'email_queue', ['created_at'])
    op.create_index('ix_email_queue_status_next_attempt', 'email_queue', ['status', 'next_attempt_at'])


def downgrade() -> None:
    op.drop_table('email_queue')

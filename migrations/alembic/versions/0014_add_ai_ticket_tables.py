"""add AI ticket tables

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-02

Creates tables for DonMatteo-AI-Tiket module:
- forum_tickets
- forum_ticket_messages
- ai_faq_articles
- ai_provider_configs

Uses checkfirst to be idempotent on fresh databases created with create_all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return inspector.has_table(name)


def upgrade() -> None:
    if not _has_table('forum_tickets'):
        op.create_table(
            'forum_tickets',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
            sa.Column('telegram_topic_id', sa.Integer(), nullable=True, index=True),
            sa.Column('status', sa.String(), server_default='open', nullable=False),
            sa.Column('ai_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table('forum_ticket_messages'):
        op.create_table(
            'forum_ticket_messages',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('forum_tickets.id', ondelete='CASCADE'), nullable=False, index=True),
            sa.Column('role', sa.String(), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('message_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if not _has_table('ai_faq_articles'):
        op.create_table(
            'ai_faq_articles',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('title', sa.String(255), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('keywords', sa.String(1024), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if not _has_table('ai_provider_configs'):
        op.create_table(
            'ai_provider_configs',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('name', sa.String(50), unique=True, nullable=False, index=True),
            sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
            sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
            sa.Column('api_keys', sa.JSON(), server_default='[]', nullable=False),
            sa.Column('active_key_index', sa.Integer(), server_default='0', nullable=False),
            sa.Column('selected_model', sa.String(255), nullable=True),
            sa.Column('available_models', sa.JSON(), server_default='[]', nullable=False),
            sa.Column('base_url', sa.String(512), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table('ai_provider_configs')
    op.drop_table('ai_faq_articles')
    op.drop_table('forum_ticket_messages')
    op.drop_table('forum_tickets')

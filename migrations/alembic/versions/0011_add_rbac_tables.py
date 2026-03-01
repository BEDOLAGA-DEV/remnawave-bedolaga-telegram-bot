"""add RBAC tables (admin_roles, user_roles, access_policies, admin_audit_log)

Revision ID: 0011
Revises: 0010
Create Date: 2026-02-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
            "WHERE table_schema = 'public' AND table_name = :name)"
        ),
        {'name': table_name},
    )
    return result.scalar()


def upgrade() -> None:
    if _has_table('admin_roles'):
        return

    op.create_table(
        'admin_roles',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(100), unique=True, nullable=False),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('level', sa.Integer, server_default=sa.text('0'), nullable=False),
        sa.Column('permissions', JSONB, server_default='[]', nullable=False),
        sa.Column('color', sa.String(7), nullable=True),
        sa.Column('icon', sa.String(50), nullable=True),
        sa.Column('is_system', sa.Boolean, server_default=sa.text('false'), nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true'), nullable=False),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'user_roles',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role_id', sa.Integer, sa.ForeignKey('admin_roles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('assigned_by', sa.Integer, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('assigned_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true'), nullable=False),
        sa.UniqueConstraint('user_id', 'role_id', name='uq_user_role'),
    )

    op.create_table(
        'access_policies',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('role_id', sa.Integer, sa.ForeignKey('admin_roles.id', ondelete='CASCADE'), nullable=True),
        sa.Column('priority', sa.Integer, server_default=sa.text('0'), nullable=False),
        sa.Column('effect', sa.String(10), nullable=False),
        sa.Column('conditions', JSONB, server_default='{}', nullable=False),
        sa.Column('resource', sa.String(100), nullable=False),
        sa.Column('actions', JSONB, server_default='[]', nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.text('true'), nullable=False),
        sa.Column('created_by', sa.Integer, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'admin_audit_log',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(100), nullable=False),
        sa.Column('resource_type', sa.String(50), nullable=True),
        sa.Column('resource_id', sa.String(100), nullable=True),
        sa.Column('details', JSONB, nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.Text, nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('request_method', sa.String(10), nullable=True),
        sa.Column('request_path', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index('ix_admin_audit_user_created', 'admin_audit_log', ['user_id', 'created_at'])
    op.create_index('ix_admin_audit_resource', 'admin_audit_log', ['resource_type', 'resource_id'])
    op.create_index('ix_admin_audit_created', 'admin_audit_log', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_admin_audit_created', table_name='admin_audit_log')
    op.drop_index('ix_admin_audit_resource', table_name='admin_audit_log')
    op.drop_index('ix_admin_audit_user_created', table_name='admin_audit_log')
    op.drop_table('admin_audit_log')
    op.drop_table('access_policies')
    op.drop_table('user_roles')
    op.drop_table('admin_roles')

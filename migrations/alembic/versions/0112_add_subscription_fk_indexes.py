"""add missing indexes on foreign keys referencing subscriptions.id

Without indexes on child tables, deleting rows from `subscriptions`
causes sequential scans on every referencing table for foreign key checks
(e.g. UPDATE subscription_events SET subscription_id = NULL WHERE subscription_id = ...),
leading to database timeouts during bulk operations such as reset_trials
and subscription_dedup_service.

Revision ID: 0112
Revises: 0111
Create Date: 2026-09-06
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0112'
down_revision: Union[str, None] = '0111'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = [
    ('ix_subscription_events_subscription_id', 'subscription_events', ['subscription_id']),
    ('ix_sent_notifications_subscription_id', 'sent_notifications', ['subscription_id']),
    ('ix_discount_offers_subscription_id', 'discount_offers', ['subscription_id']),
    ('ix_subscription_temporary_access_subscription_id', 'subscription_temporary_access', ['subscription_id']),
    ('ix_grace_access_sessions_subscription_id', 'grace_access_sessions', ['subscription_id']),
]


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            for idx_name, table_name, cols in INDEXES:
                cols_sql = ', '.join(cols)
                op.execute(
                    f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx_name} '
                    f'ON {table_name} ({cols_sql})'
                )
    else:
        for idx_name, table_name, cols in INDEXES:
            try:
                op.create_index(
                    idx_name,
                    table_name,
                    cols,
                    unique=False,
                )
            except Exception:
                pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            for idx_name, _, _ in INDEXES:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {idx_name}')
    else:
        for idx_name, table_name, _ in INDEXES:
            try:
                op.drop_index(idx_name, table_name=table_name)
            except Exception:
                pass

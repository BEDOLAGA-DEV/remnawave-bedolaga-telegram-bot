"""add ON DELETE CASCADE and index to subscription_servers.subscription_id

Revision ID: 0047
Revises: 0046
Create Date: 2026-03-23

Recreates the FK constraint on subscription_servers.subscription_id
with ON DELETE CASCADE so that deleting a subscription automatically
removes dependent subscription_servers rows. Also adds an index
on subscription_id for efficient CASCADE deletes and joins.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0047'
down_revision: str | None = '0046'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _get_actual_fk_name(connection, table: str, column: str) -> str | None:
    """Look up actual FK constraint name from pg_constraint."""
    result = connection.execute(
        text("""
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            JOIN pg_attribute att ON att.attrelid = con.conrelid
                AND att.attnum = ANY(con.conkey)
            WHERE rel.relname = :table
                AND att.attname = :column
                AND con.contype = 'f'
                AND nsp.nspname = 'public'
            LIMIT 1
        """),
        {'table': table, 'column': column},
    )
    row = result.fetchone()
    return row[0] if row else None


def upgrade() -> None:
    connection = op.get_bind()
    actual_fk = _get_actual_fk_name(connection, 'subscription_servers', 'subscription_id')
    if actual_fk:
        op.drop_constraint(actual_fk, 'subscription_servers', type_='foreignkey')
    op.create_foreign_key(
        'subscription_servers_subscription_id_fkey',
        'subscription_servers',
        'subscriptions',
        ['subscription_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_index('ix_subscription_servers_subscription_id', 'subscription_servers', ['subscription_id'])


def downgrade() -> None:
    op.drop_index('ix_subscription_servers_subscription_id', 'subscription_servers')
    connection = op.get_bind()
    actual_fk = _get_actual_fk_name(connection, 'subscription_servers', 'subscription_id')
    if actual_fk:
        op.drop_constraint(actual_fk, 'subscription_servers', type_='foreignkey')
    op.create_foreign_key(
        'subscription_servers_subscription_id_fkey',
        'subscription_servers',
        'subscriptions',
        ['subscription_id'],
        ['id'],
    )

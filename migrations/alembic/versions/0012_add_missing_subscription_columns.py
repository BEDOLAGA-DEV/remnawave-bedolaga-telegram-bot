"""add missing subscription columns

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-27

These columns were added to models.py but never got explicit migrations.
Fresh databases created via 0001 (create_all) already have them, so
we use checkfirst logic to avoid errors on those instances.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('subscriptions', 'last_webhook_update_at'):
        op.add_column(
            'subscriptions',
            sa.Column('last_webhook_update_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_column('subscriptions', 'is_daily_paused'):
        op.add_column(
            'subscriptions',
            sa.Column('is_daily_paused', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )

    if not _has_column('subscriptions', 'last_daily_charge_at'):
        op.add_column(
            'subscriptions',
            sa.Column('last_daily_charge_at', sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_column('subscriptions', 'remnawave_short_uuid'):
        op.add_column(
            'subscriptions',
            sa.Column('remnawave_short_uuid', sa.String(255), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('subscriptions', 'remnawave_short_uuid')
    op.drop_column('subscriptions', 'last_daily_charge_at')
    op.drop_column('subscriptions', 'is_daily_paused')
    op.drop_column('subscriptions', 'last_webhook_update_at')

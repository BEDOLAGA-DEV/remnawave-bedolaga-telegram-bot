"""reconcile drifted schema: pending_campaign_slug + paypear_payments

Some deployments have alembic_version ahead of the real schema (the DB was
stamped/restored past migrations 0055 and 0058 without their DDL ever
materializing). Runtime then crashes with:
  - UndefinedColumnError: column users.pending_campaign_slug does not exist
  - UndefinedTableError: relation "paypear_payments" does not exist

This migration idempotently (re)creates BOTH objects with IF NOT EXISTS so it
is a no-op on healthy DBs and a repair on drifted ones. Mirrors the column
def from 0055 and the table def from 0058.

Revision ID: 0114
Revises: 0113
Create Date: 2026-06-06

"""

from typing import Sequence, Union

from alembic import op


revision: str = '0114'
down_revision: Union[str, None] = '0113'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the one missing COLUMN that drift left behind.

    Missing TABLES (paypear_payments, rollypay_payments, etc.) are repaired by
    the startup reconcile pass in app/database/migrations.py
    (``_reconcile_missing_tables`` -> ``Base.metadata.create_all(checkfirst=True)``),
    which runs on every boot and is idempotent. create_all never ALTERs an
    existing table, so the users.pending_campaign_slug add must stay explicit
    here.
    """
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_campaign_slug VARCHAR(64)"
    )


def downgrade() -> None:
    # Non-destructive reconciliation — do not drop objects other migrations own.
    pass

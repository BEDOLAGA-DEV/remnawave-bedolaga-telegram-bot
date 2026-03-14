"""Add varchar_pattern_ops index on guest_purchases.token for prefix queries.

Revision ID: 0035
Revises: 0034
"""

from alembic import op


revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_guest_purchases_token_pattern '
        'ON guest_purchases (token varchar_pattern_ops)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_guest_purchases_token_pattern')

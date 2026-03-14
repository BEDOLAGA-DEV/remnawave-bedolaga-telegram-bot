"""Add recipient_warning column to guest_purchases.

Revision ID: 0034
Revises: 0033
"""

from alembic import op
import sqlalchemy as sa


revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('guest_purchases', sa.Column('recipient_warning', sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column('guest_purchases', 'recipient_warning')

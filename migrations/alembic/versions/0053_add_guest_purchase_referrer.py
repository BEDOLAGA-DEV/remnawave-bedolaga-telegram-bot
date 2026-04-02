"""Add referrer column to guest_purchases.

Revision ID: 0053
Revises: 0052
"""

from alembic import op
import sqlalchemy as sa

revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('guest_purchases', sa.Column('referrer', sa.String(500), nullable=True))


def downgrade():
    op.drop_column('guest_purchases', 'referrer')

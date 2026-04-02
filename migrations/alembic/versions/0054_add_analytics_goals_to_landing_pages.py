"""Add analytics goal fields to landing_pages.

Revision ID: 0054
Revises: 0053
"""

from alembic import op
import sqlalchemy as sa

revision = '0054'
down_revision = '0053'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'landing_pages', sa.Column('analytics_view_enabled', sa.Boolean(), server_default='false', nullable=True)
    )
    op.add_column(
        'landing_pages', sa.Column('analytics_view_goal', sa.String(100), server_default='landing_view', nullable=True)
    )
    op.add_column(
        'landing_pages', sa.Column('analytics_click_enabled', sa.Boolean(), server_default='false', nullable=True)
    )
    op.add_column(
        'landing_pages', sa.Column('analytics_click_goal', sa.String(100), server_default='landing_pay', nullable=True)
    )


def downgrade():
    op.drop_column('landing_pages', 'analytics_click_goal')
    op.drop_column('landing_pages', 'analytics_click_enabled')
    op.drop_column('landing_pages', 'analytics_view_goal')
    op.drop_column('landing_pages', 'analytics_view_enabled')

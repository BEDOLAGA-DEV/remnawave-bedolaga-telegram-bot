"""add trial_duration_days to tariffs

Revision ID: 0050
Revises: 0049
Create Date: 2026-03-31

"""

from alembic import op
import sqlalchemy as sa

revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='tariffs' AND column_name='trial_duration_days'"
        )
    )
    if result.fetchone() is None:
        op.add_column('tariffs', sa.Column('trial_duration_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tariffs', 'trial_duration_days')

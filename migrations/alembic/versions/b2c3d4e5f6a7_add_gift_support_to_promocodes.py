"""add gift support to promocodes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-01-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Добавляем колонку description для хранения параметров gift-подписок в формате JSON
    op.add_column('promocodes', sa.Column('description', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('promocodes', 'description')

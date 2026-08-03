"""coupon_batches.max_per_user — лимит активаций партии на пользователя

0 (по умолчанию) — без ограничения, прежнее поведение. Для раздач и конкурсов
ставится 1: один человек не сможет забрать всю партию.

Revision ID: 0106
Revises: 0105
"""

from alembic import op
import sqlalchemy as sa


revision = '0106'
down_revision = '0105'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('coupon_batches') as batch:
        batch.add_column(sa.Column('max_per_user', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('coupon_batches') as batch:
        batch.drop_column('max_per_user')

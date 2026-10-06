"""coupon_batches.max_per_user — лимит активаций партии на пользователя

0 (по умолчанию) — без ограничения, прежнее поведение. Для раздач и конкурсов
ставится 1: один человек не сможет забрать всю партию.

Revision ID: 0102
Revises: 0101
"""

from alembic import op
import sqlalchemy as sa


revision = '0102'
down_revision = '0101'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if 'coupon_batches' not in tables:
        # coupon_batches was never created (legacy slig deployments prior to upstream 0095)
        # 0095 will create the table WITH this column via create_table, so nothing to do.
        return
    existing_cols = {col['name'] for col in inspector.get_columns('coupon_batches')}
    if 'max_per_user' not in existing_cols:
        with op.batch_alter_table('coupon_batches') as batch:
            batch.add_column(sa.Column('max_per_user', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if 'coupon_batches' not in tables:
        return
    existing_cols = {col['name'] for col in inspector.get_columns('coupon_batches')}
    if 'max_per_user' in existing_cols:
        with op.batch_alter_table('coupon_batches') as batch:
            batch.drop_column('max_per_user')

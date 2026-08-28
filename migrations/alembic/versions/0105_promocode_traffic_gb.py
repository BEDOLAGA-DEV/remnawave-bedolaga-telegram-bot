"""promocodes.traffic_gb — трафик в наборе бонусов промокода

Комбинированный промокод раздавал баланс и дни подписки; трафика в наборе
не было, хотя это самый естественный третий бонус — подписка у всех уже
есть, а гигабайты добавляются к ней без смены тарифа.

0 (по умолчанию) — трафик не начисляется, прежнее поведение.

Revision ID: 0105
Revises: 0104
"""

from alembic import op
import sqlalchemy as sa


revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('promocodes') as batch:
        batch.add_column(sa.Column('traffic_gb', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('promocodes') as batch:
        batch.drop_column('traffic_gb')

"""create subscription_premium_traffic (premium squad traffic limits)

Revision ID: 0116
Revises: 0115
Create Date: 2026-09-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0116'
down_revision: Union[str, None] = '0115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subscription_premium_traffic',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'subscription_id',
            sa.Integer(),
            sa.ForeignKey('subscriptions.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('squad_uuid', sa.String(64), nullable=False),
        sa.Column('limit_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('extra_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        # Сколько списать с замера в первые сутки периода. NULL — ещё не
        # определяли: эндпоинт статистики принимает только даты без времени,
        # поэтому запрос за день начала периода захватывает и то, что потрачено
        # до сброса. Разницу снимаем один раз, первым замером.
        sa.Column('baseline_bytes', sa.BigInteger(), nullable=True),
        sa.Column('is_limited', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('period_start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('panel_reset_ack_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notified_80', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_subscription_premium_traffic_id', 'subscription_premium_traffic', ['id'])
    # Ведущая колонка — squad_uuid: воркер обходит состояния пачкой по скваду,
    # чтобы взять расход всех подписчиков одним запросом к панели.
    op.create_index('ix_subscription_premium_traffic_squad', 'subscription_premium_traffic', ['squad_uuid'])
    # Уникальность пары нужна и как инвариант, и как защита от гонки: воркер и
    # докупка премиум-трафика пишут сюда конкурентно.
    op.create_unique_constraint(
        'uq_subscription_premium_traffic_sub_squad',
        'subscription_premium_traffic',
        ['subscription_id', 'squad_uuid'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_subscription_premium_traffic_sub_squad',
        'subscription_premium_traffic',
        type_='unique',
    )
    op.drop_index('ix_subscription_premium_traffic_squad', table_name='subscription_premium_traffic')
    op.drop_index('ix_subscription_premium_traffic_id', table_name='subscription_premium_traffic')
    op.drop_table('subscription_premium_traffic')

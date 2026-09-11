"""create subscription_premium_traffic (premium squad traffic limits)

Revision ID: 0120
Revises: 0119
Create Date: 2026-09-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0120'
down_revision: Union[str, None] = '0119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'subscription_premium_traffic'


def _has_table() -> bool:
    return TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # Номер миграции менялся при слияниях с dev (0116 → 0117 → 0119 → 0120).
    # База, где она прошла под прежним номером, после перенумерации видит её
    # непройденной — повторный запуск не должен падать на готовой таблице.
    if _has_table():
        return

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
        # Уникальность пары нужна и как инвариант, и как защита от гонки: воркер и
        # докупка премиум-трафика пишут сюда конкурентно. Объявлена в самой
        # таблице, как и в модели, — отдельный ALTER не поддерживается SQLite.
        sa.UniqueConstraint('subscription_id', 'squad_uuid', name='uq_subscription_premium_traffic_sub_squad'),
    )
    op.create_index('ix_subscription_premium_traffic_id', 'subscription_premium_traffic', ['id'])
    # Ведущая колонка — squad_uuid: воркер обходит состояния пачкой по скваду,
    # чтобы взять расход всех подписчиков одним запросом к панели.
    op.create_index('ix_subscription_premium_traffic_squad', 'subscription_premium_traffic', ['squad_uuid'])


def downgrade() -> None:
    if not _has_table():
        return

    # Уникальный ключ уходит вместе с таблицей.
    op.drop_index('ix_subscription_premium_traffic_squad', table_name='subscription_premium_traffic')
    op.drop_index('ix_subscription_premium_traffic_id', table_name='subscription_premium_traffic')
    op.drop_table('subscription_premium_traffic')

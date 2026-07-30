"""add lava_subscriptions + tariffs.lava_product_id

Рекуррентные подписки Lava. В отличие от Platega сумма и периодичность заданы
продуктом в кабинете Lava, поэтому тарифу добавляется ссылка на продукт
(``tariffs.lava_product_id``), а в записи подписки сохраняется скопированный на
момент оформления шаг продления (``charge_days``).

Partial unique index создаётся сразу (в отличие от Platega, где он приехал
отдельной миграцией 0100): живых дублей на новой таблице ещё не существует.

Revision ID: 0101
Revises: 0100
"""

from alembic import op
import sqlalchemy as sa


revision = '0101'
down_revision = '0100'
branch_labels = None
depends_on = None

_ALIVE = "('PENDING', 'ACTIVE', 'PAST_DUE')"


def upgrade() -> None:
    op.create_table(
        'lava_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id'), nullable=True),
        sa.Column('lava_subscription_id', sa.String(length=255), nullable=True),
        sa.Column('lava_product_id', sa.String(length=255), nullable=False),
        sa.Column('lava_consumer_id', sa.String(length=255), nullable=True),
        sa.Column('order_id', sa.String(length=255), nullable=False),
        sa.Column('charge_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=False, server_default='RUB'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('redirect_url', sa.Text(), nullable=True),
        sa.Column('next_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_charge_external_id', sa.String(length=255), nullable=True),
        sa.Column('charges_success', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('charges_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_lava_subscriptions_user_id', 'lava_subscriptions', ['user_id'])
    op.create_index('ix_lava_subscriptions_subscription_id', 'lava_subscriptions', ['subscription_id'])
    op.create_unique_constraint('uq_lava_subscriptions_lava_id', 'lava_subscriptions', ['lava_subscription_id'])
    op.create_unique_constraint('uq_lava_subscriptions_order_id', 'lava_subscriptions', ['order_id'])
    op.create_index('ix_lava_subscriptions_user_active', 'lava_subscriptions', ['user_id', 'status'])
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_lava_subscriptions_alive
            ON lava_subscriptions (subscription_id)
            WHERE status IN {_ALIVE}
            """
        )
    )

    with op.batch_alter_table('tariffs') as batch:
        batch.add_column(sa.Column('lava_product_id', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('lava_product_id')
    op.execute(sa.text('DROP INDEX IF EXISTS uq_lava_subscriptions_alive'))
    op.drop_table('lava_subscriptions')

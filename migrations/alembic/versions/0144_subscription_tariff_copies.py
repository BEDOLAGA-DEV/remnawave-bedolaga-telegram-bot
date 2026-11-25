"""Allow several live subscriptions of the same tariff (tariff copies).

The partial unique index ``uq_subscriptions_user_tariff_active`` kept at most one
live subscription per (user, tariff). It now covers only ordinary rows
(``copy_key IS NULL``) and keeps guarding them against races: a double click or a
replayed webhook still cannot create a second ordinary subscription. A copy is an
explicit purchase and carries its own unique ``copy_key``.

Revision ID: 0144
Revises: 0143
"""

import sqlalchemy as sa
from alembic import op


revision = '0144'
down_revision = '0143'
branch_labels = None
depends_on = None

_ALIVE = "status IN ('active', 'trial', 'limited')"
_ACTIVE_INDEX = 'uq_subscriptions_user_tariff_active'
_COPY_KEY_INDEX = 'uq_subscriptions_copy_key'


def _subscription_columns() -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns('subscriptions')}


def _subscription_indexes() -> set[str]:
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('subscriptions')}


def upgrade():
    if 'copy_key' not in _subscription_columns():
        op.add_column('subscriptions', sa.Column('copy_key', sa.String(64), nullable=True))
    if _COPY_KEY_INDEX not in _subscription_indexes():
        op.create_index(_COPY_KEY_INDEX, 'subscriptions', ['copy_key'], unique=True)

    op.execute(sa.text(f'DROP INDEX IF EXISTS {_ACTIVE_INDEX}'))
    op.execute(
        sa.text(
            f'CREATE UNIQUE INDEX {_ACTIVE_INDEX} ON subscriptions (user_id, tariff_id) '
            f'WHERE tariff_id IS NOT NULL AND copy_key IS NULL AND {_ALIVE}'
        )
    )


def downgrade():
    if 'copy_key' in _subscription_columns():
        live_copy = (
            op.get_bind()
            .execute(sa.text(f'SELECT 1 FROM subscriptions WHERE copy_key IS NOT NULL AND {_ALIVE} LIMIT 1'))
            .first()
        )
        if live_copy:
            raise RuntimeError(
                'Cannot restore one live subscription per tariff: live tariff copies exist. '
                'Expire or delete them first.'
            )

    # Оба индекса ссылаются на copy_key: сначала их, потом колонку (SQLite не
    # удаляет индексированную колонку). Пересоздание таблицы через batch здесь не
    # годится — оно переносит остальные частичные индексы через отражение схемы.
    op.execute(sa.text(f'DROP INDEX IF EXISTS {_ACTIVE_INDEX}'))
    op.execute(sa.text(f'DROP INDEX IF EXISTS {_COPY_KEY_INDEX}'))
    if 'copy_key' in _subscription_columns():
        op.execute(sa.text('ALTER TABLE subscriptions DROP COLUMN copy_key'))
    op.execute(
        sa.text(
            f'CREATE UNIQUE INDEX {_ACTIVE_INDEX} ON subscriptions (user_id, tariff_id) '
            f'WHERE tariff_id IS NOT NULL AND {_ALIVE}'
        )
    )

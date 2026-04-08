"""add subscription remnawave_short_id

Revision ID: 0051
Revises: 0050
Create Date: 2026-03-19

"""

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0051'
down_revision: Union[str, None] = '0050'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _generate_short_id() -> str:
    return secrets.token_hex(3)


def upgrade() -> None:
    # 1. Add column as nullable first
    op.add_column('subscriptions', sa.Column('remnawave_short_id', sa.String(16), nullable=True))

    # 2. Backfill existing rows with unique short IDs
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id FROM subscriptions WHERE remnawave_short_id IS NULL')).fetchall()
    used_ids: set[str] = set()
    for (row_id,) in rows:
        short_id = _generate_short_id()
        while short_id in used_ids:
            short_id = _generate_short_id()
        used_ids.add(short_id)
        conn.execute(
            sa.text('UPDATE subscriptions SET remnawave_short_id = :sid WHERE id = :rid'),
            {'sid': short_id, 'rid': row_id},
        )

    # 3. Set NOT NULL + UNIQUE
    op.alter_column('subscriptions', 'remnawave_short_id', nullable=False, server_default='')
    op.create_unique_constraint('uq_subscriptions_remnawave_short_id', 'subscriptions', ['remnawave_short_id'])


def downgrade() -> None:
    op.drop_constraint('uq_subscriptions_remnawave_short_id', 'subscriptions', type_='unique')
    op.drop_column('subscriptions', 'remnawave_short_id')

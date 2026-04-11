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
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c['name'] for c in inspector.get_columns('subscriptions')}

    # 1. Add column as nullable first (if missing)
    if 'remnawave_short_id' not in existing_cols:
        op.add_column('subscriptions', sa.Column('remnawave_short_id', sa.String(16), nullable=True))

    # 2. Backfill any rows that still have NULL remnawave_short_id
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

    # 3. Set NOT NULL (safe to call repeatedly — re-inspect first to avoid no-op thrash)
    cols_info = {c['name']: c for c in inspector.get_columns('subscriptions')}
    short_id_col = cols_info.get('remnawave_short_id')
    if short_id_col and short_id_col.get('nullable', True):
        op.alter_column('subscriptions', 'remnawave_short_id', nullable=False, server_default='')

    # 4. Add UNIQUE constraint (if missing)
    unique_names = {uc['name'] for uc in inspector.get_unique_constraints('subscriptions')}
    if 'uq_subscriptions_remnawave_short_id' not in unique_names:
        op.create_unique_constraint(
            'uq_subscriptions_remnawave_short_id',
            'subscriptions',
            ['remnawave_short_id'],
        )


def downgrade() -> None:
    op.drop_constraint('uq_subscriptions_remnawave_short_id', 'subscriptions', type_='unique')
    op.drop_column('subscriptions', 'remnawave_short_id')

"""multi subscription foundation

Revision ID: 0050
Revises: 0049
Create Date: 2026-03-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0050'
down_revision: Union[str, None] = '0049'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 1. Remove UNIQUE constraint from subscriptions.user_id (if present)
    unique_constraints = inspector.get_unique_constraints('subscriptions')
    for uc in unique_constraints:
        if uc.get('name') == 'subscriptions_user_id_key':
            op.drop_constraint('subscriptions_user_id_key', 'subscriptions', type_='unique')
            break

    # Ensure regular index exists on user_id
    existing_indexes = {ix['name'] for ix in inspector.get_indexes('subscriptions')}
    if 'ix_subscriptions_user_id' not in existing_indexes:
        op.create_index('ix_subscriptions_user_id', 'subscriptions', ['user_id'])

    # 2. Add composite indexes for multi-subscription queries
    if 'ix_subscriptions_user_status' not in existing_indexes:
        op.create_index('ix_subscriptions_user_status', 'subscriptions', ['user_id', 'status'])
    if 'ix_subscriptions_user_tariff_status' not in existing_indexes:
        op.create_index(
            'ix_subscriptions_user_tariff_status',
            'subscriptions',
            ['user_id', 'tariff_id', 'status'],
        )

    # 3. Partial unique index: prevent duplicate active/trial subscriptions for same tariff.
    # Use IF NOT EXISTS for idempotency (the index cannot be represented via alembic's
    # op.create_index because it has a WHERE clause).
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_user_tariff_active
            ON subscriptions (user_id, tariff_id)
            WHERE tariff_id IS NOT NULL AND status IN ('active', 'trial')
            """
        )
    )

    # 4. Add remnawave_uuid column to subscriptions (if missing)
    existing_cols = {c['name'] for c in inspector.get_columns('subscriptions')}
    if 'remnawave_uuid' not in existing_cols:
        op.add_column(
            'subscriptions',
            sa.Column('remnawave_uuid', sa.String(255), nullable=True),
        )

        # 5. Data migration: copy User.remnawave_uuid → Subscription.remnawave_uuid
        # only when we just added the column (otherwise the backfill already ran).
        op.execute(
            sa.text(
                """
                UPDATE subscriptions
                SET remnawave_uuid = users.remnawave_uuid
                FROM users
                WHERE subscriptions.user_id = users.id
                  AND subscriptions.remnawave_short_uuid IS NOT NULL
                  AND users.remnawave_uuid IS NOT NULL
                """
            )
        )

    # 6. Change tariff_id FK from SET NULL to RESTRICT (only if currently SET NULL).
    # Look at the existing FK definition to decide whether to rewrite it.
    fks = inspector.get_foreign_keys('subscriptions')
    needs_fk_swap = False
    for fk in fks:
        if fk.get('name') != 'subscriptions_tariff_id_fkey':
            continue
        options = fk.get('options') or {}
        ondelete = (options.get('ondelete') or '').upper()
        if ondelete != 'RESTRICT':
            needs_fk_swap = True
        break
    if needs_fk_swap:
        op.drop_constraint('subscriptions_tariff_id_fkey', 'subscriptions', type_='foreignkey')
        op.create_foreign_key(
            'subscriptions_tariff_id_fkey',
            'subscriptions',
            'tariffs',
            ['tariff_id'],
            ['id'],
            ondelete='RESTRICT',
        )


def downgrade() -> None:
    # Reverse FK change: RESTRICT → SET NULL
    op.drop_constraint('subscriptions_tariff_id_fkey', 'subscriptions', type_='foreignkey')
    op.create_foreign_key(
        'subscriptions_tariff_id_fkey',
        'subscriptions',
        'tariffs',
        ['tariff_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # Remove remnawave_uuid column
    op.drop_column('subscriptions', 'remnawave_uuid')

    # Remove partial unique index
    op.execute(sa.text('DROP INDEX IF EXISTS uq_subscriptions_user_tariff_active'))

    # Remove composite indexes
    op.drop_index('ix_subscriptions_user_tariff_status', 'subscriptions')
    op.drop_index('ix_subscriptions_user_status', 'subscriptions')

    # Remove regular index
    op.drop_index('ix_subscriptions_user_id', 'subscriptions')

    # Restore UNIQUE constraint on user_id
    op.create_unique_constraint('subscriptions_user_id_key', 'subscriptions', ['user_id'])

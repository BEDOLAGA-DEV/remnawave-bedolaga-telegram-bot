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
    # 1. Remove UNIQUE constraint from subscriptions.user_id
    # First drop the unique index/constraint
    op.drop_constraint('subscriptions_user_id_key', 'subscriptions', type_='unique')

    # Ensure regular index exists on user_id (was implicit with unique)
    op.create_index('ix_subscriptions_user_id', 'subscriptions', ['user_id'])

    # 2. Add composite indexes for multi-subscription queries
    op.create_index('ix_subscriptions_user_status', 'subscriptions', ['user_id', 'status'])
    op.create_index('ix_subscriptions_user_tariff_status', 'subscriptions', ['user_id', 'tariff_id', 'status'])

    # 3. Partial unique index: prevent duplicate active/trial subscriptions for same tariff
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX uq_subscriptions_user_tariff_active
            ON subscriptions (user_id, tariff_id)
            WHERE tariff_id IS NOT NULL AND status IN ('active', 'trial')
            """
        )
    )

    # 4. Add remnawave_uuid column to subscriptions
    op.add_column('subscriptions', sa.Column('remnawave_uuid', sa.String(255), nullable=True))

    # 5. Data migration: copy User.remnawave_uuid → Subscription.remnawave_uuid
    # for existing subscriptions that have remnawave_short_uuid (i.e., linked to Remnawave)
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

    # 6. Change tariff_id FK from SET NULL to RESTRICT
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
    op.drop_index('uq_subscriptions_user_tariff_active', 'subscriptions')

    # Remove composite indexes
    op.drop_index('ix_subscriptions_user_tariff_status', 'subscriptions')
    op.drop_index('ix_subscriptions_user_status', 'subscriptions')

    # Remove regular index
    op.drop_index('ix_subscriptions_user_id', 'subscriptions')

    # Restore UNIQUE constraint on user_id
    op.create_unique_constraint('subscriptions_user_id_key', 'subscriptions', ['user_id'])

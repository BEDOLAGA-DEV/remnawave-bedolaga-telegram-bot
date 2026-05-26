"""saved_payment_methods: provider abstraction columns

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-19

Adds provider-agnostic columns to ``saved_payment_methods`` so multiple
recurring payment providers (YooKassa, EtoPlatezhi, ...) can persist
saved-card tokens in the same table. Backfills existing rows with
``provider='yookassa'`` and copies ``yookassa_payment_method_id`` into
``provider_token``. The legacy column is kept for one minor release as
an alias and will be dropped in a follow-up migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'saved_payment_methods' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}

    if 'provider' not in existing:
        op.add_column(
            'saved_payment_methods',
            sa.Column('provider', sa.String(32), nullable=False, server_default='yookassa'),
        )
    if 'provider_token' not in existing:
        op.add_column(
            'saved_payment_methods',
            sa.Column('provider_token', sa.String(255), nullable=True),
        )
    if 'valid_thru' not in existing:
        op.add_column(
            'saved_payment_methods',
            sa.Column('valid_thru', sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(
        sa.text(
            """
            UPDATE saved_payment_methods
            SET provider_token = yookassa_payment_method_id
            WHERE provider_token IS NULL
              AND yookassa_payment_method_id IS NOT NULL
            """
        )
    )

    existing_indexes = {idx['name'] for idx in inspector.get_indexes('saved_payment_methods')}
    if 'ix_saved_payment_methods_provider_token' not in existing_indexes:
        op.create_index(
            'ix_saved_payment_methods_provider_token',
            'saved_payment_methods',
            ['provider', 'provider_token'],
            unique=True,
            postgresql_where=sa.text('provider_token IS NOT NULL'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'saved_payment_methods' not in inspector.get_table_names():
        return

    existing_indexes = {idx['name'] for idx in inspector.get_indexes('saved_payment_methods')}
    if 'ix_saved_payment_methods_provider_token' in existing_indexes:
        op.drop_index('ix_saved_payment_methods_provider_token', table_name='saved_payment_methods')

    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}
    for col in ('valid_thru', 'provider_token', 'provider'):
        if col in existing:
            op.drop_column('saved_payment_methods', col)

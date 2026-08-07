"""saved_payment_methods: provider abstraction + method_code columns

Adds provider-agnostic columns to ``saved_payment_methods`` so multiple
recurring payment providers (YooKassa, EtoPlatezhi, ...) can persist
saved-card tokens in the same table:

* ``provider``       — provider name ('yookassa', 'etoplatezhi', ...).
* ``provider_token`` — unified saved-card token (backfilled from the legacy
  ``yookassa_payment_method_id`` for existing rows).
* ``valid_thru``     — provider-side saved-card expiry (nullable).
* ``method_code``    — provider-specific recurring endpoint selector
  (EtoPlatezhi: card-partner / sberpay / sbp-qr / yoomoney-wallet).

The legacy ``yookassa_payment_method_id`` column is kept as an alias but is
relaxed to NULLABLE — EtoPlatezhi saved cards carry a ``recurring_id`` in
``provider_token`` and have no YooKassa id.

Merges the historical custom revisions 0095_recurring_provider_columns and
0096_saved_method_code (including the EtoPlatezhi method_code backfill) into a
single revision layered on top of the upstream ``0104`` head. Every step is
inspector-guarded so it is safe to run against a schema created fresh via
``Base.metadata.create_all`` (which already has the columns).

Revision ID: 0105
Revises: 0104
Create Date: 2026-08-07

"""

import sqlalchemy as sa
from alembic import op


# Стиль как у соседних ревизий: alembic читает эти имена по соглашению.
revision = '0105'
down_revision = '0104'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'saved_payment_methods' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}

    # --- provider abstraction columns (was 0095) ---------------------------
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

    # --- method_code column + backfill (was 0096) --------------------------
    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}
    if 'method_code' not in existing:
        op.add_column(
            'saved_payment_methods',
            sa.Column('method_code', sa.String(64), nullable=True),
        )

    if bind.dialect.name == 'postgresql' and 'etoplatezhi_payments' in inspector.get_table_names():
        # Map our internal payment_method labels to EtoPlatezhi method codes
        # (as used in /v2/payment/{code}/recurring URL paths).
        op.execute(
            sa.text(
                """
                UPDATE saved_payment_methods spm
                SET method_code = CASE
                    WHEN ep.payment_method = 'card'     THEN 'card-partner'
                    WHEN ep.payment_method = 'sberpay'  THEN 'sberpay'
                    WHEN ep.payment_method = 'yoomoney' THEN 'yoomoney-wallet'
                    ELSE NULL
                END
                FROM etoplatezhi_payments ep
                WHERE spm.provider = 'etoplatezhi'
                  AND spm.method_code IS NULL
                  AND ep.user_id = spm.user_id
                  AND ep.is_paid = true
                  AND ep.created_at <= spm.created_at + interval '5 minutes'
                  AND ep.created_at >= spm.created_at - interval '1 hour'
                  AND ep.id = (
                      SELECT MAX(ep2.id) FROM etoplatezhi_payments ep2
                      WHERE ep2.user_id = spm.user_id
                        AND ep2.is_paid = true
                        AND ep2.created_at <= spm.created_at + interval '5 minutes'
                  )
                """
            )
        )

    # Anything still NULL for EtoPlatezhi → assume card-partner (historical default)
    op.execute(
        sa.text(
            """
            UPDATE saved_payment_methods
            SET method_code = 'card-partner'
            WHERE provider = 'etoplatezhi' AND method_code IS NULL
            """
        )
    )

    # --- relax legacy yookassa_payment_method_id to nullable ---------------
    yk_col = next(
        (c for c in inspector.get_columns('saved_payment_methods') if c['name'] == 'yookassa_payment_method_id'),
        None,
    )
    if yk_col is not None and not yk_col.get('nullable', True):
        with op.batch_alter_table('saved_payment_methods') as batch_op:
            batch_op.alter_column(
                'yookassa_payment_method_id',
                existing_type=sa.String(255),
                nullable=True,
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
    for col in ('method_code', 'valid_thru', 'provider_token', 'provider'):
        if col in existing:
            op.drop_column('saved_payment_methods', col)

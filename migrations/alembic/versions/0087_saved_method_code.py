"""saved_payment_methods: method_code for provider-specific endpoints

Revision ID: 0087
Revises: 0086
Create Date: 2026-05-25

Adds ``method_code`` to ``saved_payment_methods`` so providers that expose
distinct recurring endpoints per payment method (e.g. EtoPlatezhi's
``/v2/payment/{card-partner,sberpay,yoomoney-wallet}/recurring``) can route
charges correctly. Backfills existing EtoPlatezhi rows from the
``payment_method`` recorded on the originating ``etoplatezhi_payments`` row;
remaining EtoPlatezhi rows default to ``card-partner``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0087'
down_revision: Union[str, None] = '0086'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'saved_payment_methods' not in inspector.get_table_names():
        return

    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}
    if 'method_code' not in existing:
        op.add_column(
            'saved_payment_methods',
            sa.Column('method_code', sa.String(64), nullable=True),
        )

    if 'etoplatezhi_payments' in inspector.get_table_names():
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


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'saved_payment_methods' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('saved_payment_methods')}
    if 'method_code' in existing:
        op.drop_column('saved_payment_methods', 'method_code')

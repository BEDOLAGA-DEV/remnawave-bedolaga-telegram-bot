"""fix sbp method_code backfill missed by 0105

Ревизия 0105 бэкфиллила `saved_payment_methods.method_code` из
`etoplatezhi_payments.payment_method`, но её CASE знал только
card/sberpay/yoomoney. Метод `sbp` уходил в `ELSE NULL`, а следующий за ним
UPDATE проставлял таким строкам `card-partner`. В итоге СБП-токены значились
карточными, списание уходило на `/v2/payment/card-partner/recurring` и падало.

0105 на существующих инсталляциях уже применена, поэтому правка её тела ничего
не чинит — нужна отдельная ревизия.

Правим консервативно: только те строки, у которых ВСЕ оплаченные платежи в окне
привязки сходятся на одном методе. Если у юзера в окне встречается ещё и `card`,
привязка неоднозначна и строку не трогаем.

Revision ID: 0106
Revises: 0105
"""

import sqlalchemy as sa
from alembic import op


revision = '0106'
down_revision = '0105'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if not {'saved_payment_methods', 'etoplatezhi_payments'} <= tables:
        return
    if bind.dialect.name != 'postgresql':
        return

    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT spm.id AS spm_id,
                       array_agg(DISTINCT ep.payment_method) AS methods
                FROM saved_payment_methods spm
                JOIN etoplatezhi_payments ep
                  ON ep.user_id = spm.user_id
                 AND ep.is_paid = true
                 AND ep.created_at BETWEEN spm.created_at - interval '1 hour'
                                       AND spm.created_at + interval '5 minutes'
                WHERE spm.provider = 'etoplatezhi'
                  AND spm.method_code = 'card-partner'
                GROUP BY spm.id
            )
            UPDATE saved_payment_methods spm
            SET method_code = CASE
                    WHEN c.methods = ARRAY['sbp']::varchar[]     THEN 'sbp-qr'
                    WHEN c.methods = ARRAY['sberpay']::varchar[] THEN 'sberpay'
                END
            FROM candidates c
            WHERE spm.id = c.spm_id
              AND c.methods IN (ARRAY['sbp']::varchar[], ARRAY['sberpay']::varchar[])
            """
        )
    )


def downgrade() -> None:
    # Обратной правки нет: исходное значение было заведомо неверным
    # ('card-partner' для СБП/SberPay-токенов), возвращать его незачем.
    pass

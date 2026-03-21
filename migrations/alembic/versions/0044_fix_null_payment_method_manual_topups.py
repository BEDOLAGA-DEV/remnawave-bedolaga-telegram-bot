"""fix payment_method=NULL for admin manual top-ups

Revision ID: 0044
Revises: 0043
Create Date: 2026-03-21

Data-only migration: sets payment_method='manual' on deposit transactions
that were created by admin top-ups (Cabinet API, WebAPI, Telegram bot)
but stored with payment_method=NULL due to a bug.

Strategy: exclude all known non-admin deposit patterns that legitimately
have payment_method=NULL (wheel prizes, campaigns, promo codes, referral
purchase commissions, legacy webhook duplicates). Everything remaining
with type='deposit' AND payment_method IS NULL is an admin manual top-up.
"""

from typing import Sequence, Union

from alembic import op

revision: str = '0044'
down_revision: Union[str, None] = '0043'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE transactions
        SET payment_method = 'manual'
        WHERE type = 'deposit'
          AND payment_method IS NULL
          AND is_completed = TRUE
          AND (description IS NULL OR (
            description NOT LIKE 'Выигрыш в колесе удачи:%'
            AND description NOT LIKE 'Бонус за регистрацию по кампании%'
            AND description NOT LIKE 'Бонус по промокоду%'
            AND description NOT LIKE 'Комиссия %'
            AND description NOT LIKE 'Бонус за первое пополнение%'
            AND description NOT LIKE 'Бонус за реферала%'
            AND description NOT LIKE 'Восстановленный бонус%'
            AND description NOT LIKE 'Пополнение через Tribute%'
            AND description NOT LIKE 'Пополнение через Telegram Stars%'
          ))
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE transactions
        SET payment_method = NULL
        WHERE type = 'deposit'
          AND payment_method = 'manual'
          AND is_completed = TRUE
          AND (description IS NULL OR (
            description NOT LIKE 'Выигрыш в колесе удачи:%'
            AND description NOT LIKE 'Бонус за регистрацию по кампании%'
            AND description NOT LIKE 'Бонус по промокоду%'
            AND description NOT LIKE 'Комиссия %'
            AND description NOT LIKE 'Бонус за первое пополнение%'
            AND description NOT LIKE 'Бонус за реферала%'
            AND description NOT LIKE 'Восстановленный бонус%'
            AND description NOT LIKE 'Пополнение через Tribute%'
            AND description NOT LIKE 'Пополнение через Telegram Stars%'
          ))
    """)

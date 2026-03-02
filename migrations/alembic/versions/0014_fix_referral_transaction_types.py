"""fix historical referral transactions recorded as deposit

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-02

Data-only migration: updates transactions.type from 'deposit' to 'referral_reward'
for historical referral commission records that were incorrectly saved as deposits.
Discriminator: payment_method IS NULL (real deposits always have payment_method)
plus description pattern matching as belt-and-suspenders safety.
"""

from typing import Sequence, Union

from alembic import op

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE transactions
        SET type = 'referral_reward'
        WHERE type = 'deposit'
          AND payment_method IS NULL
          AND (
            description ILIKE '%реферал%'
            OR description ILIKE '%referral%'
            OR description ILIKE '%комиссия%'
            OR description ILIKE '%бонус за первое пополнение%'
            OR description ILIKE '%бонус за реферала%'
          )
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE transactions
        SET type = 'deposit'
        WHERE type = 'referral_reward'
          AND payment_method IS NULL
          AND (
            description ILIKE '%реферал%'
            OR description ILIKE '%referral%'
            OR description ILIKE '%комиссия%'
            OR description ILIKE '%бонус за первое пополнение%'
            OR description ILIKE '%бонус за реферала%'
          )
    """)

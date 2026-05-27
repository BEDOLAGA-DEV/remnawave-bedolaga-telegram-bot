"""guest_purchases: add referrer_code column for landing partner referral

Revision ID: 0088
Revises: 0087
Create Date: 2026-05-27

Stores the referral code (`?ref=XXX` from landing URL) so that when the
guest purchase activates and a `User` is created, we can resolve the
referral code to a partner and set `users.referred_by_id` automatically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0088'
down_revision: Union[str, None] = '0087'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'guest_purchases',
        sa.Column('referrer_code', sa.String(length=64), nullable=True),
    )
    op.create_index(
        'ix_guest_purchases_referrer_code',
        'guest_purchases',
        ['referrer_code'],
    )


def downgrade() -> None:
    op.drop_index('ix_guest_purchases_referrer_code', table_name='guest_purchases')
    op.drop_column('guest_purchases', 'referrer_code')

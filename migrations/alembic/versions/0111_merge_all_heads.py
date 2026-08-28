"""Merge all branch heads into a single head

Merges the two independent upstream v4.2.0 branch heads:

  - 0110 (referral_user_reward_choice — via 0109 merging ensure_coupon + referral_reward)
  - 5651d7089c66 (make_platega_subscriptions_user_id_nullable)

The custom slig/production-patches chain (0105c → 0106c → 0107c → 0108) is
already in the ancestry of 0110 via the 0108 merge point.

After this migration there is exactly one head: 0111.

Revision ID: 0111
Revises: 0110, 5651d7089c66
Create Date: 2026-08-28
"""

from typing import Sequence, Union


revision: str = '0111'
down_revision: Union[str, tuple] = ('0110', '5651d7089c66')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

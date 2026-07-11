"""backfill legacy partner_status='approved'

Revision ID: 0121
Revises: 0120
Create Date: 2026-07-13

Legacy users who had referral_commission_percent set before the partner
approval workflow (migration 0003) were left with partner_status='none'.
This blocked them from the /admin/partners?status=approved list even
though they are functional partners.

Marks such users as 'approved' so they surface in the admin UI.
Criteria for backfill:
  - partner_status == 'none' (default) OR NULL
  - AND (referral_commission_percent IS NOT NULL AND > 0)
        OR user has at least one referred user (referred_by_id points to them)

Idempotent: safe to re-run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0121'
down_revision: Union[str, None] = '0120'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('users', 'partner_status'):
        # partner system not installed — nothing to backfill
        return

    conn = op.get_bind()

    # Backfill 1: users with an explicit non-zero commission percent
    if _has_column('users', 'referral_commission_percent'):
        conn.execute(
            sa.text(
                """
                UPDATE users
                SET partner_status = 'approved'
                WHERE (partner_status IS NULL OR partner_status = 'none')
                  AND referral_commission_percent IS NOT NULL
                  AND referral_commission_percent > 0
                """
            )
        )

    # Backfill 2: users who already have referrals in the system
    if _has_column('users', 'referred_by_id'):
        conn.execute(
            sa.text(
                """
                UPDATE users
                SET partner_status = 'approved'
                WHERE (partner_status IS NULL OR partner_status = 'none')
                  AND id IN (
                      SELECT DISTINCT referred_by_id
                      FROM users
                      WHERE referred_by_id IS NOT NULL
                  )
                """
            )
        )


def downgrade() -> None:
    # No reliable inverse: cannot distinguish backfilled rows from
    # legitimately approved partners. Leave data untouched on downgrade.
    pass

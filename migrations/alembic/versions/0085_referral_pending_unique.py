"""partial unique on referral_earnings registration-pending rows

The cross-session race between the bot's /start handler and the
cabinet's auth endpoints can produce duplicate
``referral_registration_pending`` audit rows for the same
``(user_id, referral_id)``. After commit 418f1d56, app-level
SELECT-before-INSERT dedup narrows the window to sub-millisecond
but doesn't close it — two concurrent sessions can still both pass
the SELECT before either commits.

This migration closes the gap structurally:

  1. Collapse any duplicate ``referral_registration_pending`` rows
     left over from before the dedup landed. Strategy: keep the
     lowest-id row per ``(user_id, referral_id)``, delete the rest.
     Uses ANSI-portable subquery so the same SQL works on both
     PostgreSQL (prod) and SQLite (dev).

  2. Create a partial UNIQUE index covering only the
     ``referral_registration_pending`` rows. Other reason values
     (``referral_first_topup_bonus``, ``referral_commission_topup``,
     etc.) are intentionally allowed to repeat — each topup is a
     separate audit row.

After this migration, a second concurrent INSERT of the same
``(user_id, referral_id, 'referral_registration_pending')`` raises
``IntegrityError`` and the application's existing
SELECT-before-INSERT path swallows it gracefully.

Revision ID: 0085
Revises: 0084
Create Date: 2026-05-16
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0085'
down_revision: Union[str, None] = '0084'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Cleanup duplicates left from before the SELECT dedup.
    # ANSI-portable: works on both PostgreSQL and SQLite.
    op.execute(
        """
        DELETE FROM referral_earnings
        WHERE reason = 'referral_registration_pending'
          AND id NOT IN (
            SELECT MIN(id) FROM referral_earnings
            WHERE reason = 'referral_registration_pending'
            GROUP BY user_id, referral_id
          )
        """
    )

    # 2. Partial UNIQUE index — only enforced for registration-pending
    # rows. Other `reason` values are unaffected.
    # PostgreSQL syntax (`CREATE UNIQUE INDEX ... WHERE`) is also
    # supported by SQLite ≥ 3.8.0, which the project requires.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_referral_earnings_registration_pending
        ON referral_earnings (user_id, referral_id)
        WHERE reason = 'referral_registration_pending'
        """
    )


def downgrade() -> None:
    # Drop the index. We do NOT restore the deleted duplicates —
    # they were audit-row noise (amount_kopeks=0, reason=pending) and
    # collapsing them is the whole point.
    op.execute('DROP INDEX IF EXISTS uq_referral_earnings_registration_pending')

"""clear white-list traffic fields on existing bio-reward subscriptions

Bio-reward free subs are not supposed to carry WL traffic. Earlier rows were
created before the explicit ``wl_traffic_limit_gb=None`` override and inherited
the model default (5 GB). This migration backfills NULL/0 onto those rows.
Going forward ``app/services/bio_reward_service._create_free_sub`` sets the
WL fields explicitly so new rows are correct.

Revision ID: 0080
Revises: 0079
Create Date: 2026-05-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = '0080'
down_revision: str | None = '0079'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscriptions
           SET wl_traffic_limit_gb = NULL,
               wl_traffic_used_gb = 0,
               wl_purchased_traffic_gb = 0,
               wl_traffic_reset_at = NULL
         WHERE is_bio_reward = true
        """
    )


def downgrade() -> None:
    # Irreversible by design: the original per-row WL limit is not tracked
    # elsewhere, and resetting to a hard-coded default would be wrong for any
    # rows whose WL was correctly customised. No-op.
    pass

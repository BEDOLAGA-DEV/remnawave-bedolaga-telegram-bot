"""add performance indexes for referral network queries

Revision ID: 0041
Revises: 0040
Create Date: 2026-03-20

"""

from typing import Sequence, Union

from alembic import op

revision: str = '0041'
down_revision: Union[str, None] = '0040'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    # autocommit_block() temporarily disables the transaction wrapper.
    #
    # NOTE: If a concurrent index creation fails midway, PostgreSQL leaves behind
    # an INVALID index. Check with:
    #   SELECT indexrelname FROM pg_stat_user_indexes
    #   JOIN pg_index ON pg_index.indexrelid = pg_stat_user_indexes.indexrelid
    #   WHERE NOT pg_index.indisvalid;
    # Then drop the invalid index and re-run the migration.
    with op.get_context().autocommit_block():
        # Index on advertising_campaign_registrations(user_id, created_at)
        # Fixes sequential scan in _fetch_campaign_registrations which filters by user_id
        # and uses ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at)
        op.create_index(
            'ix_campaign_reg_user_created',
            'advertising_campaign_registrations',
            ['user_id', 'created_at'],
            if_not_exists=True,
            postgresql_concurrently=True,
        )

        # Covering composite index on transactions(user_id, type, is_completed, amount_kopeks)
        # Enables index-only scans for aggregation queries in referral network stats:
        # _fetch_personal_spent, _fetch_branch_revenue, _fetch_campaign_stats
        op.create_index(
            'ix_transactions_user_type_completed_amount',
            'transactions',
            ['user_id', 'type', 'is_completed', 'amount_kopeks'],
            if_not_exists=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_transactions_user_type_completed_amount')
        op.execute('DROP INDEX CONCURRENTLY IF EXISTS ix_campaign_reg_user_created')

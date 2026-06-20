"""composite index on users(referred_by_id, has_made_first_topup)

The tiered partner commission policy (commit 4b48d519) introduced
``get_paid_referrals_count(referrer_id)``, which executes

    SELECT COUNT(*) FROM users
    WHERE referred_by_id = $1 AND has_made_first_topup = true

on every referral commission calculation — i.e. on every paying
referral's top-up. The pre-existing single-column index on
``users.referred_by_id`` is selective enough for partners with a handful
of referrals, but for partners with thousands of referrals (campaign
landings, KOL bots) PostgreSQL has to fetch each row and re-filter on
``has_made_first_topup``.

A composite index lets the query plan as an index-only scan and keeps
tier selection O(log N) in referral count.

``CREATE INDEX CONCURRENTLY`` is used so the migration does not take an
``ACCESS EXCLUSIVE`` lock on ``users`` during the index build — matches
the repo convention established by migrations 0041, 0042, 0043, 0048,
0080, 0081 for any index on ``users`` or other large tables.
``CONCURRENTLY`` requires running outside a transaction, hence the
``autocommit_block()``. On SQLite (used in dev/CI) ``CONCURRENTLY`` is
not supported, so the migration falls back to the standard
``create_index`` path there.

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-28
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = 'ix_users_referred_by_paid'


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} '
                'ON users (referred_by_id, has_made_first_topup)'
            )
    else:
        op.create_index(
            INDEX_NAME,
            'users',
            ['referred_by_id', 'has_made_first_topup'],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}')
    else:
        op.drop_index(INDEX_NAME, table_name='users')

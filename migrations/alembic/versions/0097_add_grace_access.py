"""add grace_access_sessions table

Stores versioned billing/panel snapshots for one-shot restricted grace access.
The canonical subscription remains unchanged while the temporary RemnaWave
overlay is active.  A partial unique index guarantees at most one open grace
session per subscription, while the incident constraint prevents re-granting
the same expiry/traffic incident.

Revision ID: 0097
Revises: 0096
Create Date: 2026-07-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OPEN_STATES_SQL = "state IN ('pending', 'active', 'restoring')"


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {str(item['name']) for item in inspector.get_indexes(table_name) if item.get('name')}


def _install_delete_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == 'postgresql':
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_open_grace_subscription_delete()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM grace_access_sessions
                    WHERE subscription_id = OLD.id
                      AND state IN ('pending', 'active', 'restoring')
                ) THEN
                    RAISE EXCEPTION 'subscription has an open grace-access session'
                        USING ERRCODE = '23503';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute('DROP TRIGGER IF EXISTS trg_guard_open_grace_subscription_delete ON subscriptions')
        op.execute(
            """
            CREATE TRIGGER trg_guard_open_grace_subscription_delete
            BEFORE DELETE ON subscriptions
            FOR EACH ROW EXECUTE FUNCTION guard_open_grace_subscription_delete()
            """
        )
    elif dialect == 'sqlite':
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_guard_open_grace_subscription_delete
            BEFORE DELETE ON subscriptions
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM grace_access_sessions
                WHERE subscription_id = OLD.id
                  AND state IN ('pending', 'active', 'restoring')
            )
            BEGIN
                SELECT RAISE(ABORT, 'subscription has an open grace-access session');
            END
            """
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    subscription_columns = {column['name'] for column in inspector.get_columns('subscriptions')}
    if 'grace_candidate_reason' not in subscription_columns:
        op.add_column('subscriptions', sa.Column('grace_candidate_reason', sa.String(length=16), nullable=True))
    if 'grace_candidate_at' not in subscription_columns:
        op.add_column('subscriptions', sa.Column('grace_candidate_at', sa.DateTime(timezone=True), nullable=True))
    if 'grace_suppressed_until' not in subscription_columns:
        op.add_column('subscriptions', sa.Column('grace_suppressed_until', sa.DateTime(timezone=True), nullable=True))

    inspector = sa.inspect(bind)
    subscription_indexes = _index_names(inspector, 'subscriptions')
    if 'ix_subscriptions_grace_expiry_scan' not in subscription_indexes:
        op.create_index(
            'ix_subscriptions_grace_expiry_scan',
            'subscriptions',
            ['status', 'is_trial', 'end_date'],
            unique=False,
        )
    if 'ix_subscriptions_grace_candidate' not in subscription_indexes:
        op.create_index(
            'ix_subscriptions_grace_candidate',
            'subscriptions',
            ['grace_candidate_at', 'grace_candidate_reason'],
            unique=False,
        )

    if 'grace_access_sessions' in inspector.get_table_names():
        grace_columns = {column['name'] for column in inspector.get_columns('grace_access_sessions')}
        if 'version' not in grace_columns:
            op.add_column(
                'grace_access_sessions',
                sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
            )
        inspector = sa.inspect(bind)
        grace_indexes = _index_names(inspector, 'grace_access_sessions')
        if 'uq_grace_access_sessions_one_open' not in grace_indexes:
            op.create_index(
                'uq_grace_access_sessions_one_open',
                'grace_access_sessions',
                ['subscription_id'],
                unique=True,
                postgresql_where=sa.text(_OPEN_STATES_SQL),
                sqlite_where=sa.text(_OPEN_STATES_SQL),
            )
        if 'ix_grace_access_sessions_state_until' not in grace_indexes:
            op.create_index(
                'ix_grace_access_sessions_state_until',
                'grace_access_sessions',
                ['state', 'grace_until'],
                unique=False,
            )
        _install_delete_guard()
        return

    op.create_table(
        'grace_access_sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('remnawave_uuid', sa.String(length=255), nullable=False),
        sa.Column('reason', sa.String(length=16), nullable=False),
        sa.Column('incident_key', sa.String(length=255), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('snapshot_version', sa.Integer(), server_default=sa.text('2'), nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('billing_before', sa.JSON(), nullable=False),
        sa.Column('panel_before', sa.JSON(), nullable=False),
        sa.Column('overlay', sa.JSON(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('grace_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completion_reason', sa.String(length=16), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('expired', 'limited')",
            name='ck_grace_access_sessions_reason',
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'restoring', 'completed')",
            name='ck_grace_access_sessions_state',
        ),
        sa.CheckConstraint(
            """
            (
                state = 'completed'
                AND completion_reason IS NOT NULL
                AND completion_reason IN ('paid', 'timeout', 'drained', 'conflict', 'revoked')
                AND completed_at IS NOT NULL
            )
            OR
            (
                state <> 'completed'
                AND completion_reason IS NULL
                AND completed_at IS NULL
            )
            """,
            name='ck_grace_access_sessions_completion',
        ),
        sa.CheckConstraint(
            'grace_until > started_at',
            name='ck_grace_access_sessions_dates',
        ),
        sa.CheckConstraint(
            'snapshot_version > 0',
            name='ck_grace_access_sessions_snapshot_version',
        ),
        sa.CheckConstraint(
            'version > 0',
            name='ck_grace_access_sessions_version',
        ),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'subscription_id',
            'incident_key',
            name='uq_grace_access_sessions_incident',
        ),
    )
    op.create_index(
        'uq_grace_access_sessions_one_open',
        'grace_access_sessions',
        ['subscription_id'],
        unique=True,
        postgresql_where=sa.text(_OPEN_STATES_SQL),
        sqlite_where=sa.text(_OPEN_STATES_SQL),
    )
    op.create_index(
        'ix_grace_access_sessions_state_until',
        'grace_access_sessions',
        ['state', 'grace_until'],
        unique=False,
    )

    # Never cascade-delete the only recovery snapshot while an overlay is open.
    # Completed history may still cascade normally when a subscription is later
    # deleted.  The database guard covers ORM deletes and bulk SQL alike.
    _install_delete_guard()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    has_grace_table = 'grace_access_sessions' in inspector.get_table_names()

    if has_grace_table:
        open_count = bind.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM grace_access_sessions
                WHERE state IN ('pending', 'active', 'restoring')
                """
            )
        ).scalar_one()
        if open_count:
            raise RuntimeError(
                'Cannot downgrade while grace-access sessions are open. '
                'Switch to drain, restore/finish every open session, and retry.'
            )

    dialect = bind.dialect.name
    if dialect == 'postgresql':
        op.execute('DROP TRIGGER IF EXISTS trg_guard_open_grace_subscription_delete ON subscriptions')
        op.execute('DROP FUNCTION IF EXISTS guard_open_grace_subscription_delete()')
    elif dialect == 'sqlite':
        op.execute('DROP TRIGGER IF EXISTS trg_guard_open_grace_subscription_delete')

    if has_grace_table:
        grace_indexes = _index_names(sa.inspect(bind), 'grace_access_sessions')
        if 'ix_grace_access_sessions_state_until' in grace_indexes:
            op.drop_index('ix_grace_access_sessions_state_until', table_name='grace_access_sessions')
        if 'uq_grace_access_sessions_one_open' in grace_indexes:
            op.drop_index('uq_grace_access_sessions_one_open', table_name='grace_access_sessions')
        op.drop_table('grace_access_sessions')

    inspector = sa.inspect(bind)
    if 'ix_subscriptions_grace_candidate' in _index_names(inspector, 'subscriptions'):
        op.drop_index('ix_subscriptions_grace_candidate', table_name='subscriptions')
    inspector = sa.inspect(bind)
    if 'ix_subscriptions_grace_expiry_scan' in _index_names(inspector, 'subscriptions'):
        op.drop_index('ix_subscriptions_grace_expiry_scan', table_name='subscriptions')
    subscription_columns = {column['name'] for column in sa.inspect(bind).get_columns('subscriptions')}
    if 'grace_suppressed_until' in subscription_columns:
        op.drop_column('subscriptions', 'grace_suppressed_until')
    if 'grace_candidate_at' in subscription_columns:
        op.drop_column('subscriptions', 'grace_candidate_at')
    if 'grace_candidate_reason' in subscription_columns:
        op.drop_column('subscriptions', 'grace_candidate_reason')

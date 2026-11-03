"""Persist traffic allowances without changing existing users' quota.

Revision ID: 0139
Revises: 0138
"""

import re

import sqlalchemy as sa
from alembic import op


revision = '0139'
down_revision = '0138'
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if op.get_bind().dialect.name == 'sqlite':
        rebuild = []
        for name in ('traffic_purchases', 'transactions'):
            if name not in tables:
                continue
            ddl = (
                op.get_bind()
                .execute(sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {'name': name})
                .scalar_one()
            )
            if 'AUTOINCREMENT' not in ddl.upper():
                rebuild.append(name)
        if rebuild and op.get_bind().exec_driver_sql('PRAGMA foreign_keys').scalar_one():
            # Incoming SET NULL/CASCADE references can be modified by DROP even
            # when constraints are deferred. Alembic's dedicated SQLite engine
            # uses foreign_keys=OFF; refuse an unsafe manually supplied connection.
            raise RuntimeError('SQLite identity migration requires foreign_keys=OFF on the migration connection')
        for name in rebuild:
            # Reflection preserves rows, foreign keys, unique constraints and
            # named indexes; AUTOINCREMENT survives later cleanup of every row.
            with op.batch_alter_table(name, recreate='always', table_kwargs={'sqlite_autoincrement': True}):
                pass
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('subscriptions')}
    if 'traffic_accounting_mode' not in columns:
        op.add_column(
            'subscriptions',
            sa.Column('traffic_accounting_mode', sa.String(16), nullable=False, server_default='legacy'),
        )
    if 'traffic_accounting_states' not in tables:
        op.create_table(
            'traffic_accounting_states',
            sa.Column(
                'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='CASCADE'), primary_key=True
            ),
            sa.Column('panel_identity', sa.String(128), nullable=False),
            sa.Column('lifetime_bytes', sa.BigInteger(), nullable=False),
            sa.Column('used_bytes', sa.BigInteger(), nullable=False),
            sa.Column('revision', sa.BigInteger(), nullable=False),
            sa.Column('projected_revision', sa.BigInteger(), nullable=False, server_default='-1'),
            sa.Column('base_budget_bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('last_base_operation_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('next_delivery_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('delivery_failures', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_delivery_error', sa.String(128), nullable=True),
            sa.Column('recreation_started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('recreation_username', sa.String(36), nullable=True),
            sa.Column('recreation_previous_phase', sa.String(32), nullable=True),
            sa.Column('phase', sa.String(32), nullable=False),
            sa.Column('quota_disabled', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('activation_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('pending_sync_fields', sa.JSON(), nullable=False, server_default='[]'),
            sa.Column('pending_user_tag', sa.String(255), nullable=True),
            sa.Column('grace_excluded', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('business_strategy', sa.String(32), nullable=False),
            sa.Column('calendar_timezone', sa.String(64), nullable=False),
            sa.Column('calendar_anchor', sa.DateTime(timezone=True), nullable=False),
            sa.Column('next_refresh_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('migrated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                'lifetime_bytes >= 0 AND used_bytes >= 0 AND used_bytes <= lifetime_bytes',
                name='ck_traffic_accounting_counters',
            ),
            sa.CheckConstraint('revision >= 0', name='ck_traffic_accounting_revision'),
        )
        op.create_index(
            'ix_traffic_accounting_states_next_refresh_at', 'traffic_accounting_states', ['next_refresh_at']
        )
        op.create_index(
            'ix_traffic_accounting_states_next_delivery_at', 'traffic_accounting_states', ['next_delivery_at']
        )
    if 'traffic_allowances' not in tables:
        op.create_table(
            'traffic_allowances',
            sa.Column(
                'subscription_id',
                sa.Integer(),
                sa.ForeignKey('traffic_accounting_states.subscription_id', ondelete='CASCADE'),
                primary_key=True,
            ),
            sa.Column('operation_key', sa.String(160), primary_key=True),
            sa.Column('granted_bytes', sa.BigInteger(), nullable=False),
            sa.Column('remaining_bytes', sa.BigInteger(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                'granted_bytes >= 0 AND remaining_bytes >= 0 AND remaining_bytes <= granted_bytes',
                name='ck_traffic_allowance_amounts',
            ),
        )
        op.create_index('ix_traffic_allowances_expires_at', 'traffic_allowances', ['expires_at'])

    if op.get_bind().dialect.name == 'sqlite' and 'traffic_purchases' in tables:
        # Also safe for retrying an upgrade over a previously populated ledger:
        # deleted purchase tombstones may exceed the largest surviving row ID.
        watermark = (
            op.get_bind()
            .execute(
                sa.text("""
            SELECT COALESCE(MAX(CAST(substr(operation_key, 10) AS INTEGER)), 0)
            FROM traffic_allowances WHERE operation_key LIKE 'purchase:%'
              AND length(substr(operation_key, 10)) > 0
              AND substr(operation_key, 10) NOT GLOB '*[^0-9]*'
        """)
            )
            .scalar_one()
        )
        op.get_bind().execute(
            sa.text("""
            INSERT INTO sqlite_sequence(name, seq)
            SELECT 'traffic_purchases', :watermark
            WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='traffic_purchases')
        """),
            {'watermark': watermark},
        )
        op.get_bind().execute(
            sa.text("""
            UPDATE sqlite_sequence SET seq=MAX(seq, :watermark) WHERE name='traffic_purchases'
        """),
            {'watermark': watermark},
        )

    if 'traffic_base_grant_intents' not in tables:
        op.create_table(
            'traffic_base_grant_intents',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'subscription_id',
                sa.Integer(),
                sa.ForeignKey('traffic_accounting_states.subscription_id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('operation_key', sa.String(128), nullable=False),
            sa.Column('base_bytes', sa.BigInteger(), nullable=False),
            sa.Column('target_unlimited', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('reset_base', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('business_strategy', sa.String(32), nullable=False),
            sa.Column('tariff_id', sa.Integer(), nullable=True),
            sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('subscription_id', 'operation_key', name='uq_traffic_base_grant_operation'),
            sa.CheckConstraint('base_bytes >= 0', name='ck_traffic_base_grant_amount'),
            sa.CheckConstraint("state IN ('pending', 'applied', 'superseded')", name='ck_traffic_base_grant_state'),
        )
        op.create_index(
            'ix_traffic_base_grant_pending', 'traffic_base_grant_intents', ['subscription_id', 'state', 'id']
        )

    if op.get_bind().dialect.name == 'sqlite' and 'transactions' in tables:
        # An interrupted/retried upgrade may already contain keys of transactions
        # removed by cleanup. Their numeric identities must remain reserved.
        transaction_key = re.compile(
            r'(?:base:tariff:)?(?:admin-switch|user-switch|classic-traffic|daily-resume|classic-purchase|'
            r'miniapp-purchase|admin:buy|(?:lava|cashera|platega):payment):([1-9][0-9]{0,18})'
        )
        watermark = 0
        for table_name in ('traffic_allowances', 'traffic_base_grant_intents'):
            table = sa.table(table_name, sa.column('operation_key'))
            for key in op.get_bind().execute(sa.select(table.c.operation_key)).scalars():
                match = transaction_key.fullmatch(key)
                if match and int(match[1]) <= 2**63 - 1:
                    watermark = max(watermark, int(match[1]))
        # Preserve identities still referenced by payment/checkout history too,
        # including old databases that permitted dangling foreign keys.
        inspector = sa.inspect(op.get_bind())
        for table_name in tables:
            for foreign_key in inspector.get_foreign_keys(table_name):
                if foreign_key['referred_table'] != 'transactions' or foreign_key['referred_columns'] != ['id']:
                    continue
                column_name = foreign_key['constrained_columns'][0]
                table = sa.table(table_name, sa.column(column_name))
                highest = op.get_bind().execute(sa.select(sa.func.max(table.c[column_name]))).scalar_one()
                watermark = max(watermark, highest or 0)
        op.get_bind().execute(
            sa.text("""
            INSERT INTO sqlite_sequence(name, seq)
            SELECT 'transactions', :watermark
            WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='transactions')
        """),
            {'watermark': watermark},
        )
        op.get_bind().execute(
            sa.text("""
            UPDATE sqlite_sequence SET seq=MAX(seq, :watermark) WHERE name='transactions'
        """),
            {'watermark': watermark},
        )


def downgrade() -> None:
    if op.get_bind().execute(sa.text('SELECT 1 FROM traffic_accounting_states LIMIT 1')).first():
        raise RuntimeError('Cannot discard traffic accounting balances and operation history')
    op.drop_table('traffic_base_grant_intents')
    op.drop_table('traffic_allowances')
    op.drop_table('traffic_accounting_states')
    op.drop_column('subscriptions', 'traffic_accounting_mode')

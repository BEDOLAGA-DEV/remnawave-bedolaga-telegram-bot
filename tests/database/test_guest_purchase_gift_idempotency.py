"""Database level idempotency tests for guest_purchases idempotency_key column and index."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import GuestPurchase, GuestPurchaseStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    Tariff.__table__,
    User.__table__,
    GuestPurchase.__table__,
]


def test_guest_purchase_model_has_idempotency_key_column():
    """GuestPurchase model must have idempotency_key column and ux_guest_purchases_idempotency_key index."""
    assert 'idempotency_key' in GuestPurchase.__table__.columns
    col = GuestPurchase.__table__.columns['idempotency_key']
    assert col.nullable is True
    assert col.type.length == 64

    # Check unique index or unique constraint
    indexes = {idx.name: idx for idx in GuestPurchase.__table__.indexes}
    assert 'ux_guest_purchases_idempotency_key' in indexes
    assert indexes['ux_guest_purchases_idempotency_key'].unique is True


@pytest.mark.asyncio
async def test_multiple_null_idempotency_keys_are_allowed(monkeypatch):
    """Multiple legacy guest purchases with NULL idempotency_key must be allowed."""
    async with memory_session(monkeypatch, _TABLES) as db:
        p1 = GuestPurchase(
            token='tok_legacy_1_' + 'x' * 40,
            contact_type='telegram',
            contact_value='@user1',
            period_days=30,
            amount_kopeks=10000,
            status=GuestPurchaseStatus.PENDING.value,
            idempotency_key=None,
        )
        p2 = GuestPurchase(
            token='tok_legacy_2_' + 'x' * 40,
            contact_type='telegram',
            contact_value='@user2',
            period_days=30,
            amount_kopeks=10000,
            status=GuestPurchaseStatus.PENDING.value,
            idempotency_key=None,
        )
        db.add_all([p1, p2])
        await db.commit()

        assert p1.id is not None
        assert p2.id is not None


@pytest.mark.asyncio
async def test_duplicate_non_null_idempotency_key_is_rejected(monkeypatch):
    """Duplicate non-null idempotency_key must trigger uniqueness violation."""
    async with memory_session(monkeypatch, _TABLES) as db:
        p1 = GuestPurchase(
            token='tok_key_1_' + 'x' * 42,
            contact_type='telegram',
            contact_value='@user1',
            period_days=30,
            amount_kopeks=10000,
            status=GuestPurchaseStatus.PENDING.value,
            idempotency_key='test-idempotency-key-12345',
        )
        db.add(p1)
        await db.commit()

        p2 = GuestPurchase(
            token='tok_key_2_' + 'x' * 42,
            contact_type='telegram',
            contact_value='@user2',
            period_days=30,
            amount_kopeks=10000,
            status=GuestPurchaseStatus.PENDING.value,
            idempotency_key='test-idempotency-key-12345',
        )
        db.add(p2)
        with pytest.raises(IntegrityError):
            await db.commit()


def test_migration_0107_upgrade_downgrade_upgrade_lifecycle():
    """Verify revision 0107 upgrade, downgrade, and upgrade on a SQLite database with legacy null rows."""
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration_path = (
        Path(__file__).resolve().parents[2] / 'migrations/alembic/versions/0107_guest_purchase_idempotency.py'
    )
    spec = importlib.util.spec_from_file_location('migration_0107', migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        # Create base guest_purchases table without idempotency_key
        conn.execute(
            sa.text(
                """
                CREATE TABLE guest_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token VARCHAR(64) NOT NULL,
                    contact_type VARCHAR(20) NOT NULL,
                    contact_value VARCHAR(255) NOT NULL,
                    period_days INTEGER NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    status VARCHAR(30) NOT NULL
                )
                """
            )
        )
        # Insert legacy rows with no idempotency_key
        conn.execute(
            sa.text(
                """
                INSERT INTO guest_purchases (token, contact_type, contact_value, period_days, amount_kopeks, status)
                VALUES ('tok_legacy_1', 'telegram', '@user1', 30, 10000, 'pending'),
                       ('tok_legacy_2', 'telegram', '@user2', 30, 10000, 'pending')
                """
            )
        )

    # 1. Upgrade: add column and index
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

        inspector = sa.inspect(conn)
        cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
        assert 'idempotency_key' in cols

        indexes = {idx['name']: idx for idx in inspector.get_indexes('guest_purchases')}
        assert 'ux_guest_purchases_idempotency_key' in indexes
        assert bool(indexes['ux_guest_purchases_idempotency_key']['unique']) is True

        rows = conn.execute(sa.text('SELECT id, token, idempotency_key FROM guest_purchases ORDER BY id')).fetchall()
        assert len(rows) == 2
        assert rows[0][2] is None
        assert rows[1][2] is None

    # 2. Downgrade: drop index and column
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()

        inspector = sa.inspect(conn)
        indexes = {idx['name'] for idx in inspector.get_indexes('guest_purchases')}
        assert 'ux_guest_purchases_idempotency_key' not in indexes

    # 3. Upgrade again: column and index recreated
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

        inspector = sa.inspect(conn)
        cols = {col['name'] for col in inspector.get_columns('guest_purchases')}
        assert 'idempotency_key' in cols
        indexes = {idx['name']: idx for idx in inspector.get_indexes('guest_purchases')}
        assert 'ux_guest_purchases_idempotency_key' in indexes
        assert bool(indexes['ux_guest_purchases_idempotency_key']['unique']) is True

        rows = conn.execute(sa.text('SELECT id, token, idempotency_key FROM guest_purchases ORDER BY id')).fetchall()
        assert len(rows) == 2

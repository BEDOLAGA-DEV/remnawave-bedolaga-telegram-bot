"""Both bootstrap and upgrade preserve financial history and constraints."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, PayHotPayment, User
from tests.database.test_retired_gateway_migration_postgres import _load
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


async def test_payhot_upgrade_bootstrap_and_downgrade_guard(postgres_database):
    migration = _load('0142_create_payhot_payments.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    _load('0143_create_payhot_recurrent_journal.py').downgrade()
                    baseline = sa.inspect(sync).get_columns('payhot_payments')
                    migration.upgrade()  # Already bootstrapped metadata.
                    migration.downgrade()
                    assert not sa.inspect(sync).has_table('payhot_payments')
                    sync.execute(User.__table__.insert().values(id=1, balance_kopeks=123))
                    migration.upgrade()
                    migration.upgrade()
                    upgraded = sa.inspect(sync).get_columns('payhot_payments')
                    assert [(c['name'], str(c['type']), c['nullable']) for c in upgraded] == [
                        (c['name'], str(c['type']), c['nullable']) for c in baseline
                    ]
                    sync.execute(PayHotPayment.__table__.insert().values(user_id=1, order_id='test', amount_kopeks=100))
                    with pytest.raises(RuntimeError, match='Archive PayHot'):
                        migration.downgrade()
                    sync.execute(sa.delete(User).where(User.id == 1))
                    assert sync.scalar(sa.select(PayHotPayment.user_id)) is None
                    _load('0143_create_payhot_recurrent_journal.py').upgrade()

            await conn.run_sync(exercise)


@pytest.mark.parametrize(
    'values',
    [
        {'amount_kopeks': 0},
        {'currency': 'EUR'},
        {'amount_kopeks': -1},
    ],
)
async def test_payhot_database_rejects_invalid_money_state(postgres_database, values):
    from sqlalchemy.exc import IntegrityError

    from tests.fixtures.postgres_db import postgres_sessions

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        record = PayHotPayment(order_id='synthetic-order', amount_kopeks=10000, currency='RUB')
        for key, value in values.items():
            setattr(record, key, value)
        db.add(record)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

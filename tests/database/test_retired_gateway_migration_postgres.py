"""Retiring a gateway cleans its schema without rewriting financial records."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import Base, PaymentMethodConfig, PromoGroup, RollyPayPayment, SystemSetting, Transaction, User
from tests.fixtures.postgres_db import postgres_engine


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _load(filename):
    spec = importlib.util.spec_from_file_location(
        'migration_under_test', Path('migrations/alembic/versions') / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('existing_table', [True, False], ids=['upgrade-existing', 'fresh-metadata'])
async def test_retirement_preserves_money_and_neighbors(postgres_database, existing_table):
    migration = _load('0132_retire_paypear.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    if sa.inspect(sync).has_table('paypear_payments'):
                        sync.execute(sa.text('DROP TABLE paypear_payments'))
                    if existing_table:
                        _load('0058_create_paypear_payments.py').upgrade()
                    sync.execute(User.__table__.insert().values(id=1, telegram_id=12345, balance_kopeks=123400))
                    sync.execute(PromoGroup.__table__.insert().values(id=1, name='fixture'))
                    for method, amount in [('paypear', 123400), ('rollypay', 45600), ('severpay', 7800)]:
                        sync.execute(
                            Transaction.__table__.insert().values(
                                user_id=1,
                                type='deposit',
                                payment_method=method,
                                amount_kopeks=amount,
                                is_completed=True,
                            )
                        )
                        sync.execute(PaymentMethodConfig.__table__.insert().values(method_id=method))
                    sync.execute(
                        RollyPayPayment.__table__.insert().values(user_id=1, order_id='neighbor', amount_kopeks=45600)
                    )
                    sync.execute(
                        sa.text('INSERT INTO payment_method_promo_groups SELECT id, 1 FROM payment_method_configs')
                    )
                    for key in ['PAYPEAR_ENABLED', 'PAYPEAR_SECRET_KEY', 'ROLLYPAY_ENABLED', 'PAYPEARX_KEEP']:
                        sync.execute(SystemSetting.__table__.insert().values(key=key, value='fixture'))
                    if existing_table:
                        sync.execute(
                            sa.text(
                                "INSERT INTO paypear_payments (user_id, order_id, amount_kopeks, transaction_id) VALUES (1, 'old', 123400, 1)"
                            )
                        )
                    money_before = sync.execute(sa.select(Transaction.__table__).order_by(Transaction.id)).all()
                    migration.upgrade()
                    migration.upgrade()  # safe with a previously cleaned schema
                    assert not sa.inspect(sync).has_table('paypear_payments')
                    assert sync.execute(sa.select(Transaction.__table__).order_by(Transaction.id)).all() == money_before
                    assert sync.scalar(sa.select(User.balance_kopeks)) == 123400
                    assert (
                        sync.scalar(
                            sa.select(sa.func.sum(Transaction.amount_kopeks)).where(
                                Transaction.payment_method.in_(REAL_PAYMENT_METHODS)
                            )
                        )
                        == 176800
                    )
                    assert set(sync.scalars(sa.select(PaymentMethodConfig.method_id))) == {'rollypay', 'severpay'}
                    assert set(sync.scalars(sa.select(SystemSetting.key))) == {'ROLLYPAY_ENABLED', 'PAYPEARX_KEEP'}
                    assert sync.scalar(sa.select(sa.func.count()).select_from(RollyPayPayment)) == 1
                    assert sync.scalar(sa.text('SELECT count(*) FROM payment_method_promo_groups')) == 2
                    migration.downgrade()
                    assert sa.inspect(sync).has_table('paypear_payments')
                    assert sync.scalar(sa.text('SELECT count(*) FROM paypear_payments')) == 0
                    migration.upgrade()

            await conn.run_sync(exercise)

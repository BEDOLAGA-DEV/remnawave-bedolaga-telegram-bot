from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.database.models import AuraPayPayment, PaymentMethodConfig, PlategaPayment, Transaction, User
from app.services.merchant_fees import FeePolicy, FeeRate, get_merchant_revenue, normalize_policies, policy_at
from tests.fixtures.sqlite_memory import memory_session


def test_rounding_and_fixed_fee():
    assert FeeRate(percent='2.5', fixed_kopeks=10).calculate(10100) == 263
    assert FeeRate(percent=0).calculate(10100) == 0
    assert FeeRate(percent=100).calculate(10100) == 10100


@pytest.mark.parametrize(
    'kwargs', [{'percent': -1}, {'percent': 101}, {'percent': 'NaN'}, {'fixed_kopeks': -1}, {'percent': '1.12345'}]
)
def test_invalid_rate(kwargs):
    with pytest.raises(ValidationError):
        FeeRate(**kwargs)


def test_policy_dates_are_aware_and_unique():
    with pytest.raises(ValidationError):
        FeePolicy(effective_from='2025-01-01T00:00:00')
    with pytest.raises(ValueError):
        normalize_policies([{'effective_from': '2025-01-01T00:00:00Z'}] * 2)


def test_policy_change_does_not_affect_earlier_receipts():
    old = FeePolicy(effective_from='2025-01-01T00:00:00Z', default={'percent': 2})
    new = FeePolicy(effective_from='2025-02-01T00:00:00Z', default={'percent': 5})
    assert policy_at([new, old], datetime(2025, 1, 15, tzinfo=UTC)) == old
    assert policy_at([new, old], new.effective_from) == new
    assert policy_at([new, old], datetime(2024, 12, 31, tzinfo=UTC)) is None


@pytest.mark.asyncio
async def test_revenue_per_sub_method_and_no_balance_double_count(monkeypatch):
    tables = [
        User.__table__,
        Transaction.__table__,
        PaymentMethodConfig.__table__,
        PlategaPayment.__table__,
        AuraPayPayment.__table__,
    ]
    now = datetime(2025, 3, 1, tzinfo=UTC)
    async with memory_session(monkeypatch, tables) as db:
        user = User(telegram_id=12345)
        db.add(user)
        await db.flush()
        db.add(
            PaymentMethodConfig(
                method_id='platega',
                merchant_fee_policies=normalize_policies(
                    [
                        {
                            'effective_from': '2025-01-01T00:00:00Z',
                            'default': {'percent': 5},
                            'sub_methods': {'2': {'percent': 2}, '11': {'percent': 4, 'fixed_kopeks': 100}},
                        }
                    ]
                ),
            )
        )
        transactions = []
        for method, kind, completed in [
            ('platega', 'deposit', True),
            ('platega', 'deposit', True),
            ('platega', 'deposit', False),
            ('balance', 'subscription_payment', True),
            ('manual', 'deposit', True),
            ('aurapay', 'deposit', True),
            ('platega', 'subscription_payment', True),
            ('platega', 'refund', True),
        ]:
            transaction = Transaction(
                user_id=user.id,
                type=kind,
                payment_method=method,
                amount_kopeks=10000,
                is_completed=completed,
                created_at=now,
            )
            db.add(transaction)
            transactions.append(transaction)
        await db.flush()
        for index, code in [(0, 2), (1, 11)]:
            db.add(
                PlategaPayment(
                    user_id=user.id,
                    correlation_id=f'order-{index}',
                    amount_kopeks=10000,
                    payment_method_code=code,
                    transaction_id=transactions[index].id,
                )
            )
        await db.flush()
        report = await get_merchant_revenue(db, now - timedelta(days=1), now + timedelta(days=1))
        assert report['count'] == 4
        assert report['gross_kopeks'] == 40000
        assert report['fee_kopeks'] == 1200  # SBP 200 + card 500 + unknown method default 500
        assert report['net_kopeks'] == 38800
        assert report['unconfigured_count'] == 1
        assert report['unknown_sub_method_count'] == 1
        assert {(g['provider'], g['sub_method']) for g in report['by_method']} == {
            ('platega', '2'),
            ('platega', '11'),
            ('platega', None),
            ('aurapay', None),
        }
        assert transactions[0].amount_kopeks == 10000

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.database.models import Base, Subscription, Tariff, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('devices', [0, 2])
@pytest.mark.parametrize('custom_period', [False, True])
async def test_quote_then_create_never_grants_before_payment(
    postgres_database, monkeypatch, custom_period, devices, multi
):
    from app.config import settings
    from app.services import cispay_recurrent_checkout as checkout, tariff_purchase_quote as quotes
    from app.services.cispay_service import cispay_service

    saved = {}

    async def save(user_id, channel, terms):
        saved.update(terms)
        from datetime import UTC, datetime, timedelta

        return UUID('00000000-0000-4000-8000-000000000010'), datetime.now(UTC) + timedelta(minutes=5)

    monkeypatch.setattr(quotes, 'save_purchase_quote', save)
    monkeypatch.setattr(quotes, 'load_purchase_quote', AsyncMock(side_effect=lambda *_: dict(saved)))
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    from app.services import cispay_recurrent_service as recurrent

    monkeypatch.setattr(recurrent, 'deliver_cispay_charge', AsyncMock())
    remote = dict(
        id='00000000-0000-4000-8000-000000000020',
        status='PENDING',
        customer_id='user-1',
        amount=10000,
        currency='RUB',
        payment_method='SBP',
        interval_days=10,
        payment_url='https://pay.example/start',
    )
    create = AsyncMock(return_value=remote)
    monkeypatch.setattr(cispay_service, 'create_subscription', create)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user = User(id=1, telegram_id=1, status='active')
        db.add(user)
        db.add(
            Tariff(
                id=1,
                name='Test',
                device_limit=devices,
                period_prices={} if custom_period else {'10': 10000},
                traffic_limit_gb=0,
                custom_days_enabled=custom_period,
                price_per_day_kopeks=1000,
                min_days=7,
                max_days=360,
            )
        )
        await db.commit()
        quote = await checkout.quote_cispay_purchase(db, user=user, tariff_id=1, period_days=10, channel='cabinet')
        assert quote['amount_kopeks'] == 10000
        assert quote['first_payment_required'] is True
        result = await checkout.purchase_cispay_quote(
            db,
            user=user,
            quote_id=quote['quote_id'],
            checkout_id='00000000-0000-4000-8000-000000000030',
            channel='cabinet',
        )
        target = await db.get(Subscription, result['subscription_id'])
        assert target.status == 'expired'
        assert target.is_trial is False
        again = await checkout.purchase_cispay_quote(
            db,
            user=user,
            quote_id=quote['quote_id'],
            checkout_id='00000000-0000-4000-8000-000000000030',
            channel='cabinet',
        )
        assert again['subscription_id'] == target.id
        create.assert_awaited_once()
        # Reloading the purchase page must not start a second bank agreement
        # simply because the first target is still an expired, unpaid stub.
        with pytest.raises(ValueError, match=r'recurrent_already_exists|select_existing_subscription'):
            await checkout.quote_cispay_purchase(db, user=user, tariff_id=1, period_days=10, channel='cabinet')

        from datetime import UTC, datetime, timedelta

        from sqlalchemy import func, select

        from app.database.models import CisPaySubscriptionCharge

        payload = dict(
            id='00000000-0000-4000-8000-000000000040',
            subscription_id=remote['id'],
            store_id='shop',
            status='PAID',
            amount=10000,
            currency='RUB',
            payment_method='SBP',
            is_sandbox=False,
            paid_at=datetime.now(UTC).isoformat(),
        )
        await recurrent.process_cispay_subscription_charge(db, payload)
        await db.refresh(target)
        assert target.status == 'active'
        assert target.is_trial is False
        assert target.device_limit == devices
        assert target.end_date > datetime.now(UTC) + timedelta(days=9)
        end_date = target.end_date
        await recurrent.process_cispay_subscription_charge(db, payload)
        await db.refresh(target)
        assert target.end_date == end_date
        assert await db.scalar(select(func.count()).select_from(CisPaySubscriptionCharge)) == 1

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    Base,
    GuestPurchase,
    GuestPurchaseStatus,
    Subscription,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
)
from app.handlers.admin import users as admin
from app.services import guest_purchase_service as guest, subscription_service
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    ('flow', 'expired'), [('admin', False), ('admin', True), ('activate', False), ('activate', True), ('fulfill', True)]
)
@pytest.mark.parametrize('reset', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
async def test_paid_existing_purchase_preserves_traffic_contract(
    postgres_database, monkeypatch, flow, reset, expired, multi, trial
):
    from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason
    from app.utils.cache import cache

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    panel_call = AsyncMock(return_value=SimpleNamespace(id=123))
    panel_type = type('Panel', (), {'create_remnawave_user': panel_call})
    monkeypatch.setattr(subscription_service, 'SubscriptionService', panel_type)
    monkeypatch.setattr(guest, 'SubscriptionService', panel_type)
    for name in [
        'send_guest_notification',
        '_send_admin_notification',
        '_create_nalogo_receipt_for_purchase',
        '_attribute_purchase_campaign',
    ]:
        monkeypatch.setattr(guest, name, AsyncMock())
    monkeypatch.setattr(cache, 'get', AsyncMock(return_value=None))
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(
            id=42,
            telegram_id=42,
            first_name='Test',
            language='ru',
            auth_type='telegram',
            status='active',
            balance_kopeks=10000,
            remnawave_id=123,
        )
        tariff = Tariff(
            id=1,
            name='Test',
            is_active=True,
            traffic_limit_gb=100,
            device_limit=2,
            period_prices={'30': 300},
            allowed_squads=['test-squad'],
        )
        sub = Subscription(
            id=7,
            user_id=42,
            tariff_id=1,
            is_trial=trial,
            status='expired' if expired else 'active',
            start_date=now - timedelta(days=25),
            end_date=now + timedelta(days=-1 if expired else 5),
            traffic_limit_gb=120,
            purchased_traffic_gb=20,
            traffic_used_gb=17.5,
            device_limit=2,
            connected_squads=['test-squad'],
            remnawave_id=123,
            subscription_url='https://example.com/test-subscription',
        )
        db.add_all([user, tariff, sub])
        await db.commit()
        db.add(TrafficPurchase(id=9, subscription_id=7, traffic_gb=20, expires_at=now + timedelta(days=60)))
        await db.commit()
        if flow == 'admin':
            monkeypatch.setattr(
                admin.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': sub})
            )
            callback = SimpleNamespace(
                data='admin_buy_tariff_execute_42_1_30_300',
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
                bot=None,
            )
            handler = admin.admin_buy_tariff_execute
            while hasattr(handler, '__wrapped__'):
                handler = handler.__wrapped__
            await handler(callback, user, db)
        else:
            purchase = GuestPurchase(
                token='t' * 64,
                user_id=42,
                tariff_id=1,
                period_days=30,
                amount_kopeks=300,
                contact_type='telegram',
                contact_value='42',
                is_gift=False,
                payment_method='balance',
                status=GuestPurchaseStatus.PAID.value
                if flow == 'fulfill'
                else GuestPurchaseStatus.PENDING_ACTIVATION.value,
            )
            db.add(purchase)
            await db.commit()
            monkeypatch.setattr(guest, '_find_or_create_user', AsyncMock(return_value=(user, False)))
            monkeypatch.setattr(
                guest,
                'evaluate_guest_purchase_registration',
                AsyncMock(
                    return_value=(None, RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED))
                ),
            )
            operation = guest.fulfill_purchase if flow == 'fulfill' else guest.activate_purchase
            await operation(db, purchase.token)
            await operation(db, purchase.token)
            assert purchase.status == GuestPurchaseStatus.DELIVERED.value
        await db.refresh(user)
        await db.refresh(sub)
        assert len((await db.scalars(select(Subscription))).all()) == 1
        assert sub.traffic_used_gb == (0 if reset or trial else 17.5)
        assert sub.is_trial is False
        assert sub.traffic_limit_gb == 120
        assert sub.purchased_traffic_gb == 20
        assert await db.get(TrafficPurchase, 9) is not None
        assert sub.subscription_url == 'https://example.com/test-subscription'
        panel_call.assert_awaited_once()
        assert panel_call.await_args.kwargs.get('reset_traffic', False) is (reset or trial)
        assert user.balance_kopeks == (9700 if flow == 'admin' else 10000)
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1
        assert transactions[0].amount_kopeks == -300


@pytest.mark.parametrize(
    ('kind', 'multi', 'expired'),
    [
        ('new', False, False),
        ('new', True, False),
        ('switch', False, False),
        ('switch', False, True),
        ('gift', False, False),
        ('gift', True, False),
        ('gift', False, True),
        ('gift', True, True),
    ],
)
async def test_guest_nonrenewal_keeps_existing_panel_reset_policy(postgres_database, monkeypatch, kind, multi, expired):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
    panel_call = AsyncMock(return_value=SimpleNamespace(id=123))
    monkeypatch.setattr(guest, 'SubscriptionService', type('Panel', (), {'create_remnawave_user': panel_call}))
    for name in ['send_guest_notification', '_send_admin_notification']:
        monkeypatch.setattr(guest, name, AsyncMock())
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=42, telegram_id=42, first_name='Test', language='ru', auth_type='telegram', status='active'),
                Tariff(
                    id=1,
                    name='Target',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=2,
                    period_prices={'30': 300},
                    allowed_squads=['test-squad'],
                ),
                Tariff(
                    id=2, name='Other', is_active=True, traffic_limit_gb=100, device_limit=2, period_prices={'30': 300}
                ),
            ]
        )
        await db.commit()
        if kind != 'new':
            db.add(
                Subscription(
                    id=7,
                    user_id=42,
                    tariff_id=2 if kind == 'switch' else 1,
                    is_trial=False,
                    status='expired' if expired else 'active',
                    start_date=now - timedelta(days=25),
                    end_date=now + timedelta(days=-1 if expired else 5),
                    traffic_limit_gb=100,
                    traffic_used_gb=17.5,
                    device_limit=2,
                    connected_squads=['test-squad'],
                    remnawave_id=123,
                )
            )
        purchase = GuestPurchase(
            token='g' * 64,
            user_id=42,
            tariff_id=1,
            period_days=30,
            amount_kopeks=300,
            contact_type='telegram',
            contact_value='42',
            is_gift=kind == 'gift',
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
        )
        db.add(purchase)
        await db.commit()
        await guest.activate_purchase(db, purchase.token)
        assert purchase.status == GuestPurchaseStatus.DELIVERED.value
        panel_call.assert_awaited_once()
        assert panel_call.await_args.kwargs.get('reset_traffic', False) is False

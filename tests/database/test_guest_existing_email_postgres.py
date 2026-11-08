"""#2985: a paid landing purchase must reach the existing email owner's subscription."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.cabinet.routes.landing import _build_purchase_status_response
from app.config import settings
from app.database.models import Base, EtoplatezhiPayment, GuestPurchase, Subscription, Tariff, Transaction, User
from app.services import guest_purchase_service as guest
from app.services.payment.etoplatezhi import EtoplatezhiPaymentMixin
from app.utils.cache import cache
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('history', 'old_tariff', 'owner_auth'),
    [
        ('expired_trial', None, 'email'),
        ('expired_trial', 1, 'email'),
        ('expired_paid', 1, 'email'),
        ('active_paid', 1, 'email'),
        ('new', None, 'email'),
        ('expired_trial', None, 'telegram'),
        ('active_paid', 1, 'telegram'),
    ],
)
async def test_etoplatezhi_guest_purchase_reaches_email_owner(
    postgres_database, monkeypatch, multi, history, old_tariff, owner_auth
):
    # Catches identity duplication, skipped trial conversion, wrong owner and duplicate delivery/ledger.
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(cache, 'get', AsyncMock(return_value=None))
    for name in ['send_guest_notification', '_send_admin_notification', '_create_nalogo_receipt_for_purchase']:
        monkeypatch.setattr(guest, name, AsyncMock())

    async def provision(_self, db, subscription, **kwargs):
        # External panel boundary only: preserve real identity resolution and subscription CRUD.
        subscription.subscription_url = 'https://vpn.example/sub/test'
        await db.commit()
        return SimpleNamespace(id=9001)

    monkeypatch.setattr(guest.SubscriptionService, 'create_remnawave_user', provision)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        tariff = Tariff(
            id=1,
            name='Test',
            is_active=True,
            traffic_limit_gb=100,
            device_limit=2,
            period_prices={'30': 29900},
            allowed_squads=['test-squad'],
        )
        db.add(tariff)
        if history != 'new':
            db.add(
                User(
                    id=42,
                    email='buyer@example.com',
                    auth_type=owner_auth,
                    telegram_id=101 if owner_auth == 'telegram' else None,
                    password_hash='test-only-hash',
                    email_verified=True,
                    language='ru',
                    status='active',
                    balance_kopeks=500,
                )
            )
            await db.commit()
            db.add(
                Subscription(
                    id=7,
                    user_id=42,
                    tariff_id=old_tariff,
                    is_trial=history == 'expired_trial',
                    status='active' if history == 'active_paid' else 'expired',
                    start_date=now - timedelta(days=31),
                    end_date=now + timedelta(days=5 if history == 'active_paid' else -1),
                    traffic_limit_gb=10,
                    device_limit=1,
                    connected_squads=['test-squad'],
                )
            )
        purchase = GuestPurchase(
            token='p' * 64,
            tariff_id=1,
            period_days=30,
            amount_kopeks=29900,
            contact_type='email',
            contact_value='buyer@example.com',
            is_gift=False,
            payment_method='etoplatezhi',
            status='pending',
        )
        payment = EtoplatezhiPayment(
            order_id='test-guest-payment',
            amount_kopeks=29900,
            status='success',
            is_paid=True,
            metadata_json={'purpose': 'guest_purchase', 'purchase_token': purchase.token},
        )
        db.add_all([purchase, payment])
        await db.commit()
        handler = EtoplatezhiPaymentMixin()
        for _ in range(2):
            locked = await db.scalar(
                select(EtoplatezhiPayment).where(EtoplatezhiPayment.id == payment.id).with_for_update()
            )
            assert await handler._finalize_etoplatezhi_payment(db, locked, trigger='test')
        await db.refresh(purchase, attribute_names=['user', 'tariff'])
        users = (await db.scalars(select(User))).all()
        assert len(users) == 1
        owner = users[0]
        assert owner.email == 'buyer@example.com' and owner.status == 'active'
        assert purchase.user_id == owner.id
        if history != 'new':
            assert owner.id == 42 and owner.balance_kopeks == 500
        subscriptions = (await db.scalars(select(Subscription))).all()
        transactions = (await db.scalars(select(Transaction))).all()
        response = _build_purchase_status_response(purchase)
        if history == 'active_paid':
            assert purchase.status == response.status == 'pending_activation'
            assert len(subscriptions) == 1 and subscriptions[0].id == 7
            assert subscriptions[0].end_date == now + timedelta(days=5)
            assert transactions == []
            await guest.activate_purchase(db, purchase.token)
            await guest.activate_purchase(db, purchase.token)
            await db.refresh(subscriptions[0])
            assert purchase.status == 'delivered'
            assert subscriptions[0].end_date == now + timedelta(days=35)
            transactions = (await db.scalars(select(Transaction))).all()
            assert len(transactions) == 1
            assert transactions[0].user_id == 42 and transactions[0].amount_kopeks == -29900
        else:
            assert purchase.status == response.status == 'delivered'
            paid = [sub for sub in subscriptions if sub.status == 'active' and not sub.is_trial]
            assert len(paid) == 1 and paid[0].user_id == owner.id and paid[0].tariff_id == 1
            assert now + timedelta(days=29) < paid[0].end_date < now + timedelta(days=31)
            assert paid[0].traffic_limit_gb == 100 and paid[0].device_limit == 2
            assert response.subscription_url == 'https://vpn.example/sub/test'
            assert response.cabinet_email == 'buyer@example.com'
            assert len(transactions) == 1
            assert transactions[0].user_id == owner.id and transactions[0].amount_kopeks == -29900
            assert transactions[0].external_id == 'test-guest-payment'
            keeps_old_paid_row = multi and old_tariff is None and history not in ['new', 'expired_trial']
            assert len(subscriptions) == (2 if keeps_old_paid_row else 1)
            if history == 'expired_trial':
                assert paid[0].id == 7  # Convert the trial in place, including the legacy tariff-less case.
        await db.refresh(payment)
        assert payment.is_paid and payment.status == 'success'


async def test_paid_guest_main_conflict_is_held_without_provisioning(postgres_database, monkeypatch):
    """Оплаченный Main сохраняется для активации, не выдаёт второй Main."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(cache, 'get', AsyncMock(return_value=None))
    for name in (
        'send_guest_notification',
        '_send_admin_notification',
        '_create_nalogo_receipt_for_purchase',
        '_attribute_purchase_campaign',
    ):
        monkeypatch.setattr(guest, name, AsyncMock())
    provision = AsyncMock()
    monkeypatch.setattr(guest.SubscriptionService, 'create_remnawave_user', provision)

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=42,
                email='main-buyer@example.com',
                auth_type='email',
                password_hash='test-only-hash',
                email_verified=True,
                language='ru',
                status='active',
                balance_kopeks=500,
            )
        )
        db.add_all(
            [
                Tariff(
                    id=1,
                    name='Main Basic',
                    is_active=True,
                    entitlement_family='main',
                    period_prices={'30': 29900},
                    allowed_squads=['test-squad'],
                ),
                Tariff(
                    id=2,
                    name='Main Premium',
                    is_active=True,
                    entitlement_family='main',
                    period_prices={'30': 39900},
                    allowed_squads=['test-squad'],
                ),
            ]
        )
        await db.commit()
        db.add(
            Subscription(
                id=7,
                user_id=42,
                tariff_id=1,
                status='active',
                is_trial=False,
                start_date=datetime.now(UTC) - timedelta(days=1),
                end_date=datetime.now(UTC) + timedelta(days=29),
                connected_squads=['test-squad'],
            )
        )
        db.add(
            GuestPurchase(
                token='m' * 64,
                tariff_id=2,
                period_days=30,
                amount_kopeks=39900,
                contact_type='email',
                contact_value='main-buyer@example.com',
                payment_method='etoplatezhi',
                payment_id='paid-main-conflict',
                is_gift=False,
                status='paid',
            )
        )
        await db.commit()

        result = await guest.fulfill_purchase(db, 'm' * 64)
        assert result.status == 'pending_activation'
        assert result.user_id == 42
        provision.assert_not_awaited()
        subscriptions = (await db.scalars(select(Subscription).where(Subscription.user_id == 42))).all()
        assert len(subscriptions) == 1 and subscriptions[0].tariff_id == 1

        with pytest.raises(guest.GuestPurchaseError) as error:
            await guest.activate_purchase(db, 'm' * 64)
        assert error.value.status_code == 409
        await db.rollback()
        await db.refresh(result)
        assert result.status == 'pending_activation'
        assert (await db.scalar(select(User.balance_kopeks).where(User.id == 42))) == 500
        provision.assert_not_awaited()


async def test_paid_daily_guest_order_uses_purchasable_price(postgres_database, monkeypatch):
    """Суточная оплаченная покупка не отвергается из-за пустого period_prices."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(cache, 'get', AsyncMock(return_value=None))
    for name in (
        'send_guest_notification',
        '_send_admin_notification',
        '_create_nalogo_receipt_for_purchase',
        '_attribute_purchase_campaign',
    ):
        monkeypatch.setattr(guest, name, AsyncMock())

    async def provision(_self, db, subscription, **kwargs):
        subscription.subscription_url = 'https://vpn.example/sub/daily'
        await db.commit()
        return SimpleNamespace(id=9001)

    monkeypatch.setattr(guest.SubscriptionService, 'create_remnawave_user', provision)

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=43,
                email='daily-buyer@example.com',
                auth_type='email',
                password_hash='test-only-hash',
                email_verified=True,
                language='ru',
                status='active',
                balance_kopeks=100,
            )
        )
        db.add(
            Tariff(
                id=3,
                name='Main Daily',
                entitlement_family='main',
                is_active=True,
                is_daily=True,
                daily_price_kopeks=5000,
                period_prices={},
                allowed_squads=['test-squad'],
            )
        )
        db.add(
            GuestPurchase(
                token='d' * 64,
                tariff_id=3,
                period_days=1,
                amount_kopeks=5000,
                contact_type='email',
                contact_value='daily-buyer@example.com',
                payment_method='etoplatezhi',
                payment_id='paid-daily',
                status='paid',
                is_gift=False,
            )
        )
        await db.commit()

        result = await guest.fulfill_purchase(db, 'd' * 64)
        assert result.status == 'delivered'
        assert result.subscription_url == 'https://vpn.example/sub/daily'
        subscriptions = (await db.scalars(select(Subscription).where(Subscription.user_id == 43))).all()
        assert len(subscriptions) == 1
        assert subscriptions[0].tariff_id == 3
        assert subscriptions[0].status == 'active'
        assert (await db.scalar(select(User.balance_kopeks).where(User.id == 43))) == 100

import pytest
from unittest.mock import AsyncMock, patch
from tests.services.test_platega_subscription_callbacks import _memory_session

from app.database.models import GuestPurchase, GuestPurchaseStatus, Subscription, User
from app.database.crud.landing import create_guest_purchase
from app.database.crud.tariff import create_tariff
from app.services.payment_service import PaymentService
from app.services.payment.platega import PlategaPaymentMixin


@pytest.mark.asyncio
async def test_guest_purchase_platega_recurrent_flow(monkeypatch):
    """Test full flow: guest purchase -> Platega recurrent payment creation -> callback -> account binding."""
    async with _memory_session(monkeypatch) as db:
        # Create additional required tables
        from app.database.models import Base, GuestPurchase, LandingPage, Tariff, PlategaPayment, PromoGroup, User, Subscription, Transaction, tariff_promo_groups
        async with db.bind.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        GuestPurchase.__table__,
                        LandingPage.__table__,
                        Tariff.__table__,
                        PlategaPayment.__table__,
                        PromoGroup.__table__,
                        User.__table__,
                        Subscription.__table__,
                        Transaction.__table__,
                        tariff_promo_groups,
                    ],
                )
            )

        # 1. Create tariff
        tariff = await create_tariff(
            db,
            name='Test Recurrent Tariff',
            period_prices={30: 1000},
            is_active=True,
        )

        # 2. Create guest purchase
        guest_purchase = await create_guest_purchase(
            db,
            tariff_id=tariff.id,
            contact_type='email',
            contact_value='guest_recurrent@example.com',
            amount_kopeks=1000,
            period_days=30,
            status=GuestPurchaseStatus.PENDING,
        )

        mock_platega_service = AsyncMock()
        mock_platega_service.create_subscription.return_value = {
            'transactionId': 'pl_rec_tx_12345',
            'redirect': 'https://pay.platega.io/rec_12345',
        }

        payment_service = PaymentService()
        payment_service.platega_service = mock_platega_service

        # 3. Create guest payment via payment_service
        res = await payment_service.create_guest_payment(
            db=db,
            purchase_token=guest_purchase.token,
            payment_method='platega_recurrent',
            amount_kopeks=1000,
            description='Guest Recurrent Purchase',
            return_url='https://example.com/return',
        )

        assert res is not None
        assert res['provider'] == 'platega'
        assert res['payment_url'] == 'https://pay.platega.io/rec_12345'

        # Verify PlategaSubscription record created with null user_id and subscription_id
        from app.database.crud.platega_subscription import get_platega_subscription_by_platega_id
        sub_rec = await get_platega_subscription_by_platega_id(db, 'pl_rec_tx_12345')
        assert sub_rec is not None
        assert sub_rec.user_id is None
        assert sub_rec.subscription_id is None
        assert sub_rec.status == 'PENDING'

        # 4. Simulate callback with mock try_fulfill_guest_purchase
        callback_payload = {
            'Status': 'CONFIRMED',
            'SubscriptionId': 'pl_rec_tx_12345',
            'Id': 'charge_9999',
            'NextChargeAt': '2026-09-17T00:00:00Z',
        }

        mixin = PlategaPaymentMixin()
        mixin.platega_service = mock_platega_service

        # Create user & subscription to simulate guest fulfillment
        user = User(email='guest_recurrent@example.com', telegram_id=None)
        db.add(user)
        await db.commit()
        await db.refresh(user)

        from datetime import UTC, datetime, timedelta
        subscription = Subscription(
            user_id=user.id,
            status='active',
            tariff_id=tariff.id,
            end_date=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        guest_purchase.status = GuestPurchaseStatus.DELIVERED.value
        guest_purchase.user_id = user.id
        await db.commit()

        async def fake_fulfill(*args, **kwargs):
            return True

        with patch('app.services.payment.common.try_fulfill_guest_purchase', side_effect=fake_fulfill):
            await mixin.process_platega_subscription_callback(db, callback_payload)

        # 5. Verify results after callback
        await db.refresh(sub_rec)
        assert sub_rec.status == 'ACTIVE'
        assert sub_rec.user_id == user.id
        assert sub_rec.subscription_id == subscription.id
        assert sub_rec.last_charge_external_id == 'charge_9999'


@pytest.mark.asyncio
async def test_guest_purchase_all_tariffs_platega_recurrent(monkeypatch):
    """Verify that any tariff period (e.g. 7, 30, 90 days) with default 'platega' method creates a recurrent subscription."""
    async with _memory_session(monkeypatch) as db:
        from app.database.models import Base, GuestPurchase, LandingPage, Tariff, PlategaPayment, PromoGroup, User, Subscription, Transaction, tariff_promo_groups
        async with db.bind.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c,
                    tables=[
                        GuestPurchase.__table__,
                        LandingPage.__table__,
                        Tariff.__table__,
                        PlategaPayment.__table__,
                        PromoGroup.__table__,
                        User.__table__,
                        Subscription.__table__,
                        Transaction.__table__,
                        tariff_promo_groups,
                    ],
                )
            )

        tariff = await create_tariff(
            db,
            name='Multi Period Tariff',
            period_prices={7: 300, 30: 1000, 90: 2500},
            is_active=True,
        )

        for period_days in [7, 30, 90]:
            guest_purchase = await create_guest_purchase(
                db,
                tariff_id=tariff.id,
                contact_type='email',
                contact_value=f'guest_{period_days}@example.com',
                amount_kopeks=1000,
                period_days=period_days,
                status=GuestPurchaseStatus.PENDING,
            )

            mock_platega_service = AsyncMock()
            mock_platega_service.create_subscription.return_value = {
                'transactionId': f'pl_rec_tx_{period_days}',
                'redirect': f'https://pay.platega.io/rec_{period_days}',
            }

            payment_service = PaymentService()
            payment_service.platega_service = mock_platega_service

            res = await payment_service.create_guest_payment(
                db=db,
                purchase_token=guest_purchase.token,
                payment_method='platega',
                amount_kopeks=1000,
                description=f'Guest Purchase {period_days} days',
                return_url='https://example.com/return',
            )

            assert res is not None
            assert res['provider'] == 'platega'
            assert mock_platega_service.create_subscription.called, f"create_subscription was not called for period {period_days}"

            from app.database.crud.platega_subscription import get_platega_subscription_by_platega_id
            sub_rec = await get_platega_subscription_by_platega_id(db, f'pl_rec_tx_{period_days}')
            assert sub_rec is not None
            assert sub_rec.charge_days == period_days

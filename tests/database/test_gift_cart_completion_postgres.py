"""Actual gift debit/ledger completion must preserve a concurrently saved cart."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, GuestPurchase, Subscription, SystemSetting, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.gift_purchase_service import GIFT_ENABLED_KEY, purchase_gift_from_balance
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('replacement_mode', [None, 'gift_purchase', 'tariff_purchase'])
async def test_committed_gift_cleanup_preserves_new_cart_and_single_debit(
    postgres_database, carts, monkeypatch, replacement_mode
):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(auto, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    now = datetime.now(UTC)
    checkout = uuid4().hex
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        db.add(User(id=uid, telegram_id=uid, language='ru', status='active', balance_kopeks=1000))
        db.add(
            Tariff(
                id=1,
                name='Gift',
                is_active=True,
                show_in_gift=True,
                device_limit=1,
                traffic_limit_gb=100,
                period_prices={'30': 100},
            )
        )
        db.add(
            Subscription(
                id=10,
                user_id=uid,
                tariff_id=None,
                status='active',
                is_trial=False,
                remnawave_short_id='test-classic',
                device_limit=3,
                end_date=now + timedelta(days=10),
            )
        )
        await db.commit()
        user = await get_user_by_id(db, uid)
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'tariff_id': 1, 'return_to_cart': True}
        )
        independent = await service.get_subscription_cart(uid, 10)
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase',
                'gift_checkout_id': checkout,
                'tariff_id': 1,
                'period_days': 30,
                'total_price': 100,
                'user_id': uid,
                'return_to_cart': True,
            },
        )
        original = await service.get_user_cart(uid)
        replacement = None

        async def deliver(**kwargs):
            nonlocal replacement
            # Gift payment has committed before result delivery yields to a
            # different checkout request. Storage is real Valkey throughout.
            assert (await db.get(User, uid)).balance_kopeks == 900
            if replacement_mode:
                await service.save_user_cart(
                    uid,
                    {
                        'cart_mode': replacement_mode,
                        'gift_checkout_id': uuid4().hex,
                        'tariff_id': 1,
                        'period_days': 30,
                        'total_price': 100,
                        'subscription_id': 20 if replacement_mode == 'tariff_purchase' else None,
                        'user_id': uid,
                        'return_to_cart': True,
                    },
                )
                replacement = await service.get_user_cart(uid)
            return SimpleNamespace(message_id=1)

        monkeypatch.setattr(auto, 'send_gift_result_message', deliver)
        assert await auto.auto_purchase_saved_cart_after_topup(db, user, bot=AsyncMock())
        assert await service.get_user_cart(uid) == replacement
        assert await service.has_topup_intent(uid, independent)
        assert not await service.has_topup_intent(uid, original)
        if replacement_mode:
            assert await service.has_topup_intent(uid, replacement)
        # A retry of the original durable gift checkout must not debit again.
        replay = await purchase_gift_from_balance(
            db,
            buyer_id=uid,
            tariff_id=1,
            period_days=30,
            expected_price_kopeks=100,
            idempotency_key=checkout,
            source='bot',
        )
        assert replay.is_idempotent_replay
        await db.refresh(user)
        assert user.balance_kopeks == 900
        gifts = list(await db.scalars(select(GuestPurchase)))
        assert len(gifts) == 1 and gifts[0].status == 'paid'
        ledger = list(await db.scalars(select(Transaction)))
        assert len(ledger) == 1 and ledger[0].type == 'gift_payment' and ledger[0].amount_kopeks == -100
        classic = await db.get(Subscription, 10)
        assert (classic.tariff_id, classic.device_limit, classic.end_date) == (None, 3, now + timedelta(days=10))
        assert await service.get_user_cart(uid) == replacement

"""Restored Telegram checkout validates selection and recomputes price before UI."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services.pricing_engine import pricing_engine
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    ('mode', 'case'),
    [
        (mode, case)
        for mode in ['tariff_purchase', 'daily_tariff_purchase', 'extend']
        for case in [
            'enough',
            'insufficient',
            'promo100',
            'foreign',
            'missing',
            'corrupt_source',
            'corrupt_days',
            'invalid_period',
        ]
        if mode != 'daily_tariff_purchase' or case not in ['corrupt_days', 'invalid_period']
    ],
)
async def test_restore_validates_and_requotes(postgres_database, monkeypatch, mode, case):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    saved = AsyncMock()
    monkeypatch.setattr(telegram.user_cart_service, 'save_user_cart', saved)
    bad = case in ['foreign', 'missing', 'corrupt_source', 'corrupt_days', 'invalid_period']
    if bad:
        pricing = AsyncMock(side_effect=AssertionError('Invalid cart reached pricing'))
        monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    balance_kopeks=150 if case == 'insufficient' else 1000,
                    promo_offer_discount_percent=100 if case == 'promo100' else 0,
                    promo_offer_discount_expires_at=now + timedelta(days=1),
                ),
                User(id=2, telegram_id=202),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    is_daily=mode == 'daily_tariff_purchase',
                    daily_price_kopeks=100,
                    period_prices={'30': 300},
                ),
                Tariff(id=2, name='Neighbor', is_active=True, device_limit=7),
            ]
        )
        for sub_id, owner, tariff_id in [(10, 1, 1 if mode == 'extend' else None), (20, 1, 2), (21, 2, None)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=tariff_id,
                    is_trial=False,
                    status='active',
                    device_limit=3 if sub_id == 10 else 7,
                    remnawave_short_id=f'test-{sub_id}',
                    end_date=now + timedelta(days=10),
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        values = {'active_subscription_id': 20, 'target_subscription_id': 20}

        async def update_data(**kwargs):
            values.update(kwargs)

        state = SimpleNamespace(
            get_data=AsyncMock(side_effect=lambda: values.copy()), update_data=AsyncMock(side_effect=update_data)
        )
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        cart = {
            'cart_mode': mode,
            'tariff_id': 1,
            'subscription_id': 21
            if case == 'foreign'
            else 999
            if case == 'missing'
            else 'oops'
            if case == 'corrupt_source'
            else 10,
            'period_days': 'oops' if case == 'corrupt_days' else 90 if case == 'invalid_period' else 30,
            'total_price': 100,
            'discount_percent': 100 if case == 'promo100' else 0,
        }
        await telegram.return_to_saved_tariff_cart(callback, state, user, db, cart)
        if bad:
            pricing.assert_not_awaited()
            callback.message.edit_text.assert_not_awaited()
            assert callback.answer.await_args.kwargs['show_alert']
            saved.assert_not_awaited()
            assert values['target_subscription_id'] == 20
        else:
            expected = 0 if case == 'promo100' else 300 if mode == 'daily_tariff_purchase' else 500
            assert values['target_subscription_id'] == 10 and values['active_subscription_id'] is None
            assert values['final_price'] == expected
            rendered = callback.message.edit_text.await_args.args[0]
            assert telegram.format_price_kopeks(expected) in rendered
            if case == 'insufficient':
                saved.assert_awaited_once()
                updated = saved.await_args.args[1]
                assert updated['subscription_id'] == 10 and updated['device_limit'] == 3
                assert updated['total_price'] == expected
                assert updated['missing_amount'] == expected - 150 and updated['return_to_cart']
                keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
                assert all(
                    'confirm' not in (button.callback_data or '') for row in keyboard.inline_keyboard for button in row
                )
            else:
                assert 'Устройств: 3' in rendered
        assert (await db.scalars(select(Transaction))).all() == []
        assert user.balance_kopeks == (150 if case == 'insufficient' else 1000)
        assert (await db.get(Subscription, 20)).tariff_id == 2

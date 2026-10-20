"""#3010: backing out of a paid-trial invoice must not expose a subscription."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import multi_tariff, status
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Transaction, User
from app.handlers.subscription import my_subscriptions, purchase
from app.localization.texts import get_texts
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('view', ['api_list', 'api_info', 'api_detail', 'telegram', 'telegram_detail', 'paid_control'])
async def test_unpaid_trial_invoice_stays_hidden_after_back(postgres_database, monkeypatch, multi, view):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 100)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 0)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr('app.services.payment_service.PaymentService', lambda bot: SimpleNamespace())
    callback = SimpleNamespace(
        data='trial_payment_stars',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
        from_user=SimpleNamespace(id=101),
        bot=SimpleNamespace(send_invoice=AsyncMock()),
    )
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, first_name='Test', status='active', language='ru', balance_kopeks=500))
        await db.commit()
        user = await get_user_by_id(db, 1)
        await purchase.activate_trial(callback, user, db)
        assert (await db.scalars(select(Subscription))).all() == []
        await purchase.handle_trial_payment_method(callback, user, db)
        rows = (await db.scalars(select(Subscription))).all()
        assert len(rows) == 1
        draft = rows[0]
        assert draft.is_pending_trial
        assert draft.remnawave_id is None
        user = await get_user_by_id(db, 1)
        assert user.subscription is None
        assert not user.is_trial_already_used()
        assert user.balance_kopeks == 500
        assert (await db.scalars(select(Transaction))).all() == []
        # Reopening payment uses the same draft; leaving must not consume trial eligibility.
        await purchase.handle_trial_payment_method(callback, user, db)
        assert len((await db.scalars(select(Subscription))).all()) == 1
        callback.message.edit_text.reset_mock()
        if view == 'paid_control':
            from app.handlers.stars_payments import _handle_trial_payment
            from app.services.subscription_service import SubscriptionService

            monkeypatch.setattr(
                SubscriptionService, 'create_remnawave_user', AsyncMock(return_value=SimpleNamespace(id=9001))
            )
            monkeypatch.setattr(
                'app.services.admin_notification_service.AdminNotificationService.send_trial_activation_notification',
                AsyncMock(),
            )
            monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0)
            message = SimpleNamespace(answer=AsyncMock(), bot=SimpleNamespace())
            assert await _handle_trial_payment(message, db, user, 1, f'trial_{draft.id}', get_texts('ru'))
            await db.refresh(draft)
            assert draft.status == 'active' and draft.is_trial
            assert [row.id for row in (await multi_tariff.list_subscriptions(user, db)).subscriptions] == [draft.id]
            assert (await status.get_subscription(user, db, draft.id)).has_subscription
            user = await get_user_by_id(db, 1)
            assert user.is_trial_already_used()
            assert user.balance_kopeks == 500
            transactions = (await db.scalars(select(Transaction))).all()
            assert len(transactions) == 1 and transactions[0].amount_kopeks == -100
        elif view == 'api_list':
            response = await multi_tariff.list_subscriptions(user, db)
            assert response.subscriptions == []
        elif view == 'api_info':
            response = await status.get_subscription(user, db, draft.id)
            assert response.has_subscription is False
        elif view == 'api_detail':
            with pytest.raises(HTTPException) as exc:
                await multi_tariff.get_subscription_detail(draft.id, user, db)
            assert exc.value.status_code == 404
        elif view == 'telegram':
            await purchase.show_subscription_info(callback, user, db)
            text = callback.message.edit_text.await_args.args[0]
            if multi:
                assert 'У вас нет подписок' in text
            else:
                assert text == get_texts('ru').SUBSCRIPTION_NONE
        else:
            callback.data = f'sub_view:{draft.id}'
            state = SimpleNamespace(update_data=AsyncMock())
            await my_subscriptions.show_subscription_detail(callback, user, db, state)
            assert callback.answer.await_args.args[0] == 'Подписка не найдена'
        await db.refresh(draft)
        assert draft.is_pending_trial is (view != 'paid_control')
        assert draft.remnawave_id is None

"""Тесты для сервиса и эндпоинта принудительного отключения всех рекуррентов пользователя
через API платёжных систем.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cabinet.routes import admin_users
from app.database.models import (
    AntilopayPayment,
    AntilopayRecurrent,
    Base,
    LavaSubscription,
    PlategaSubscription,
    SavedPaymentMethod,
    Subscription,
    User,
)
from app.services.admin_recurring_cancellation_service import (
    cancel_all_user_recurring_subscriptions,
)


def _ensure_real_aiosqlite(monkeypatch) -> None:
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def _memory_session(monkeypatch):
    _ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    tables = [
        User.__table__,
        Subscription.__table__,
        PlategaSubscription.__table__,
        LavaSubscription.__table__,
        AntilopayRecurrent.__table__,
        AntilopayPayment.__table__,
        SavedPaymentMethod.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


# =========================================================================
# Unit tests for cancel_all_user_recurring_subscriptions service
# =========================================================================


@pytest.mark.asyncio
async def test_cancel_all_recurring_user_not_found(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        result = await cancel_all_user_recurring_subscriptions(db, user_id=999)
        assert result['success'] is False
        assert result['summary']['total_actions'] == 0
        assert 'не найден' in result['message']


@pytest.mark.asyncio
async def test_cancel_all_recurring_no_recurring_records(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345, username='testuser')
        db.add(user)
        await db.commit()

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)
        assert result['success'] is True
        assert result['summary']['total_actions'] == 0
        assert result['summary']['success_count'] == 0
        assert result['summary']['failed_count'] == 0
        assert len(result['results']) == 0
        assert 'не найдено' in result['message']


@pytest.mark.asyncio
async def test_cancel_all_recurring_platega_calls_api_and_updates_db(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345, username='testuser')
        db.add(user)
        now = datetime.now(timezone.utc)
        sub = Subscription(id=10, user_id=1, status='active', end_date=now)
        db.add(sub)
        p_rec = PlategaSubscription(
            id=1,
            user_id=1,
            subscription_id=10,
            platega_subscription_id='platega_sub_123',
            interval=3,
            charge_days=30,
            amount_kopeks=10000,
            status='CANCELLED',  # DB already CANCELLED, but API should STILL be called!
        )
        db.add(p_rec)
        await db.commit()

        mock_platega_service = MagicMock()
        mock_platega_service.is_configured = True
        mock_platega_service.cancel_subscription = AsyncMock(return_value={'status': 'ok'})
        monkeypatch.setattr('app.services.platega_service.platega_service', mock_platega_service)

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)

        mock_platega_service.cancel_subscription.assert_awaited_once_with('platega_sub_123', return_status=True)
        assert result['success'] is True
        assert result['summary']['total_actions'] == 1
        assert result['summary']['success_count'] == 1
        assert result['results'][0]['provider'] == 'platega'
        assert result['results'][0]['status'] == 'success'
        assert 'успешно отменена' in result['results'][0]['message']

        # DB verification
        await db.refresh(p_rec)
        assert p_rec.status == 'CANCELLED'


@pytest.mark.asyncio
async def test_cancel_all_recurring_platega_handles_api_failure(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345)
        db.add(user)
        p_rec = PlategaSubscription(
            id=1,
            user_id=1,
            subscription_id=None,
            platega_subscription_id='platega_sub_fail',
            interval=3,
            charge_days=30,
            amount_kopeks=10000,
            status='ACTIVE',
        )
        db.add(p_rec)
        await db.commit()

        mock_platega_service = MagicMock()
        mock_platega_service.is_configured = True
        mock_platega_service.cancel_subscription = AsyncMock(return_value=None)  # None = error
        monkeypatch.setattr('app.services.platega_service.platega_service', mock_platega_service)

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)

        assert result['success'] is False
        assert result['summary']['failed_count'] == 1
        assert result['results'][0]['status'] == 'error'


@pytest.mark.asyncio
async def test_cancel_all_recurring_lava_calls_api_and_updates_db(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345)
        db.add(user)
        l_rec = LavaSubscription(
            id=1,
            user_id=1,
            subscription_id=10,
            lava_product_id='prod_1',
            order_id='order_123',
            lava_subscription_id='lava_sub_456',
            charge_days=30,
            amount_kopeks=10000,
            status='ACTIVE',
        )
        db.add(l_rec)
        await db.commit()

        mock_lava_service = MagicMock()
        mock_lava_service.is_configured = True
        mock_lava_service.unsubscribe_recurrent = AsyncMock(return_value={'status': 'ok'})
        monkeypatch.setattr('app.services.lava_service.lava_service', mock_lava_service)

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)

        mock_lava_service.unsubscribe_recurrent.assert_awaited_once_with(
            subscription_id='lava_sub_456',
            order_id=None,
        )
        assert result['success'] is True
        assert result['summary']['success_count'] == 1
        assert result['results'][0]['provider'] == 'lava'
        assert result['results'][0]['status'] == 'success'

        await db.refresh(l_rec)
        assert l_rec.status == 'CANCELLED'


@pytest.mark.asyncio
async def test_cancel_all_recurring_antilopay_calls_api(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345)
        db.add(user)
        ant_rec = AntilopayRecurrent(
            id=1,
            user_id=1,
            recurrent_id='anti_rec_777',
            initial_payment_id='apay_initial_111',
            is_active=True,
            status='ACTIVE',
        )
        db.add(ant_rec)
        await db.commit()

        mock_ant_service = MagicMock()
        mock_ant_service.is_configured = True
        mock_ant_service.cancel_recurrent_payment = AsyncMock(return_value={'code': 0})
        monkeypatch.setattr('app.services.antilopay_service.antilopay_service', mock_ant_service)

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)

        mock_ant_service.cancel_recurrent_payment.assert_awaited_once_with(
            recurrent_id='anti_rec_777',
            transaction_id='apay_initial_111',
        )
        assert result['success'] is True
        assert result['summary']['success_count'] == 1
        assert result['results'][0]['provider'] == 'antilopay'
        assert result['results'][0]['status'] == 'success'

        await db.refresh(ant_rec)
        assert ant_rec.is_active is False
        assert ant_rec.status == 'CANCEL'


@pytest.mark.asyncio
async def test_cancel_all_recurring_yookassa_and_bot_autopay(monkeypatch):
    async with _memory_session(monkeypatch) as db:
        user = User(id=1, telegram_id=12345)
        db.add(user)
        now = datetime.now(timezone.utc)
        sub = Subscription(
            id=10,
            user_id=1,
            status='active',
            autopay_enabled=True,
            end_date=now,
        )
        db.add(sub)
        card = SavedPaymentMethod(
            id=1,
            user_id=1,
            yookassa_payment_method_id='pm_yoo_123',
            card_last4='4242',
            card_type='Visa',
            is_active=True,
        )
        db.add(card)
        await db.commit()

        result = await cancel_all_user_recurring_subscriptions(db, user_id=1)

        assert result['success'] is True
        assert result['summary']['total_actions'] == 2
        assert result['summary']['success_count'] == 2

        # Verify card deactivated
        await db.refresh(card)
        assert card.is_active is False

        # Verify bot autopay disabled
        await db.refresh(sub)
        assert sub.autopay_enabled is False


# =========================================================================
# Route tests for POST /{user_id}/cancel-all-recurring
# =========================================================================


@pytest.mark.asyncio
async def test_admin_cancel_all_recurring_route_success(monkeypatch):
    db = AsyncMock()
    admin = SimpleNamespace(id=99)
    user_id = 42

    user = User(id=user_id, telegram_id=12345)
    monkeypatch.setattr('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=user))

    mock_report = {
        'success': True,
        'summary': {'total_actions': 2, 'success_count': 2, 'failed_count': 0},
        'results': [
            {
                'provider': 'platega',
                'provider_title': 'Platega (СБП)',
                'target_id': 'ps-1',
                'status': 'success',
                'message': 'Отменено в API Platega',
            }
        ],
        'message': 'Все рекуррентные подписки успешно отключены',
    }
    monkeypatch.setattr(
        'app.services.admin_recurring_cancellation_service.cancel_all_user_recurring_subscriptions',
        AsyncMock(return_value=mock_report),
    )

    response = await admin_users.admin_cancel_all_user_recurring(
        user_id=user_id,
        admin=admin,
        db=db,
    )

    assert response['success'] is True
    assert response['summary']['total_actions'] == 2
    assert response['summary']['success_count'] == 2
    assert len(response['results']) == 1


@pytest.mark.asyncio
async def test_admin_cancel_all_recurring_route_user_404(monkeypatch):
    db = AsyncMock()
    admin = SimpleNamespace(id=99)
    user_id = 9999

    monkeypatch.setattr('app.cabinet.routes.admin_users.get_user_by_id', AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.admin_cancel_all_user_recurring(
            user_id=user_id,
            admin=admin,
            db=db,
        )

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    assert exc_info.value.detail == 'User not found'

"""Докупка из сохранённой корзины — на настоящей базе, без заглушки покупки.

Дополняет ``tests/handlers/test_addon_cart_resume.py``: там покупка подменена и
проверяется маршрут. Здесь сама покупка настоящая — списание с баланса,
начисление гигабайт/устройств, транзакция, очистка корзины и метки намерения —
и после тихого пополнения, и по кнопке. Подменены только панель Remnawave и
уведомления админам.
"""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, TrafficPurchase, Transaction, TransactionType, User
from app.services import subscription_auto_purchase_service as auto_service
from app.services.user_cart_service import UserCartService
from app.utils.pricing_utils import calculate_prorated_price
from tests.fixtures.sqlite_memory import memory_session


USER_ID = 7
SUB_ID = 42
TRAFFIC_PRICE = 5000
DEVICE_PRICE = 1000
BALANCE = 100_000


class _MockRedis:
    def __init__(self):
        self.storage: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        self.storage[key] = value
        return True

    async def get(self, key):
        return self.storage.get(key)

    async def delete(self, key):
        return 1 if self.storage.pop(key, None) is not None else 0

    async def exists(self, key):
        return 1 if key in self.storage else 0

    async def scan(self, cursor=0, match=None, count=50):
        return 0, [k for k in self.storage if match is None or fnmatch.fnmatch(k, match)]


class _PanelStub:
    """Панель Remnawave в тесте недоступна — синхронизация считается успешной."""

    async def update_remnawave_user(self, db, subscription):
        return None

    async def enable_remnawave_user(self, remnawave_id):
        return None


class _AdminNotifyStub:
    def __init__(self, bot):
        self.bot = bot

    async def send_subscription_update_notification(self, *args, **kwargs):
        return None


@pytest.fixture
def cart_service(monkeypatch):
    service = UserCartService()
    service._redis_client = _MockRedis()
    service._initialized = True
    monkeypatch.setattr(auto_service, 'user_cart_service', service)
    return service


@pytest.fixture(autouse=True)
def _classic_mode(monkeypatch):
    """Классический режим продаж: цена пакета из настроек, одна подписка на человека."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', DEVICE_PRICE)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)
    # Методы pydantic-модели подменяются на классе — поля у экземпляра нет.
    monkeypatch.setattr(type(settings), 'get_traffic_topup_price', lambda self, gb: TRAFFIC_PRICE if gb == 100 else 0)
    monkeypatch.setattr(type(settings), 'is_auto_purchase_after_topup_enabled', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(auto_service, 'SubscriptionService', _PanelStub)
    monkeypatch.setattr(auto_service, 'AdminNotificationService', _AdminNotifyStub)


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


async def _seed(db) -> tuple[User, Subscription]:
    now = datetime.now(UTC)
    user = User(
        id=USER_ID,
        telegram_id=555000111,
        first_name='U',
        status='active',
        language='ru',
        balance_kopeks=BALANCE,
        created_at=now,
    )
    subscription = Subscription(
        id=SUB_ID,
        user_id=USER_ID,
        status='active',
        is_trial=False,
        start_date=now - timedelta(days=15),
        end_date=now + timedelta(days=15),
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
    )
    db.add_all([user, subscription])
    await db.commit()
    return user, subscription


def _traffic_cart() -> dict:
    return {
        'cart_mode': 'add_traffic',
        'subscription_id': SUB_ID,
        'traffic_gb': 100,
        'price_kopeks': TRAFFIC_PRICE,
        'source': 'bot',
        'return_to_cart': True,
    }


def _devices_cart() -> dict:
    return {
        'cart_mode': 'add_devices',
        'subscription_id': SUB_ID,
        'devices_to_add': 2,
        'price_kopeks': DEVICE_PRICE * 2,
        'source': 'bot',
        'return_to_cart': True,
    }


async def _state(db) -> SimpleNamespace:
    db.expire_all()
    user = await db.get(User, USER_ID)
    subscription = await db.get(Subscription, SUB_ID)
    payments = (
        (
            await db.execute(
                select(Transaction).where(
                    Transaction.user_id == USER_ID,
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                )
            )
        )
        .scalars()
        .all()
    )
    purchases = (await db.execute(select(TrafficPurchase))).scalars().all()
    return SimpleNamespace(
        balance=user.balance_kopeks,
        traffic=subscription.traffic_limit_gb,
        devices=subscription.device_limit,
        payments=payments,
        purchases=purchases,
        end_date=subscription.end_date,
    )


# ---------------------------------------------------------------------------
# Тихая автопокупка после пополнения — как в жалобе, только теперь срабатывает
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topup_really_buys_traffic_from_saved_cart(monkeypatch, cart_service):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, subscription = await _seed(db)
        await cart_service.save_user_cart(USER_ID, _traffic_cart())
        expected_price, _ = calculate_prorated_price(TRAFFIC_PRICE, subscription.end_date)

        succeeded = await auto_service.auto_purchase_saved_cart_after_topup(db, user, bot=None)
        after = await _state(db)

    assert succeeded is True
    assert after.traffic == 200, 'гигабайты начислены'
    assert BALANCE - after.balance == expected_price, 'списана пропорциональная цена пакета'
    # Списания хранятся со знаком минус.
    assert [abs(p.amount_kopeks) for p in after.payments] == [expected_price]
    assert [p.traffic_gb for p in after.purchases] == [100]
    assert await cart_service.get_user_cart(USER_ID) is None, 'корзина очищена'
    assert await cart_service.has_topup_intent(USER_ID) is False, 'намерение погашено'


@pytest.mark.asyncio
async def test_topup_really_buys_devices_from_saved_cart(monkeypatch, cart_service):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, _ = await _seed(db)
        await cart_service.save_user_cart(USER_ID, _devices_cart())

        succeeded = await auto_service.auto_purchase_saved_cart_after_topup(db, user, bot=None)
        after = await _state(db)

    assert succeeded is True
    assert after.devices == 3, 'два устройства добавлены к одному'
    charged = BALANCE - after.balance
    # 2 устройства × цена × остаток дней / 30, но не меньше рубля.
    assert 100 <= charged <= DEVICE_PRICE * 2
    assert charged == abs(after.payments[0].amount_kopeks)
    assert await cart_service.get_user_cart(USER_ID) is None


# ---------------------------------------------------------------------------
# По кнопке — та же покупка, сообщение без «автоматически»
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_really_buys_traffic_and_says_so(monkeypatch, cart_service):
    bot = _bot()
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, _ = await _seed(db)
        cart = _traffic_cart()
        await cart_service.save_user_cart(USER_ID, cart)

        succeeded = await auto_service.resume_addon_cart(db, user, cart, bot=bot)
        after = await _state(db)

    assert succeeded is True
    assert after.traffic == 200
    text = bot.send_message.call_args.kwargs['text']
    assert text.startswith('✅ <b>Трафик добавлен!</b>')
    assert 'автоматически' not in text
    assert await cart_service.get_user_cart(USER_ID) is None


@pytest.mark.asyncio
async def test_button_really_buys_devices_and_says_so(monkeypatch, cart_service):
    bot = _bot()
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, _ = await _seed(db)
        cart = _devices_cart()
        await cart_service.save_user_cart(USER_ID, cart)

        succeeded = await auto_service.resume_addon_cart(db, user, cart, bot=bot)
        after = await _state(db)

    assert succeeded is True
    assert after.devices == 3
    text = bot.send_message.call_args.kwargs['text']
    assert text.startswith('✅ <b>Устройства добавлены!</b>')
    assert 'автоматически' not in text


# ---------------------------------------------------------------------------
# Денег не хватает — ничего не списано, корзина и намерение живы до следующего пополнения
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_topup_keeps_cart_and_intent(monkeypatch, cart_service):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user, _ = await _seed(db)
        user.balance_kopeks = 100
        await db.commit()
        await cart_service.save_user_cart(USER_ID, _traffic_cart())

        succeeded = await auto_service.auto_purchase_saved_cart_after_topup(db, user, bot=None)
        after = await _state(db)

    assert succeeded is False
    assert after.balance == 100 and after.traffic == 100 and after.payments == []
    assert await cart_service.get_user_cart(USER_ID) is not None, 'корзина ждёт следующего пополнения'
    assert await cart_service.has_topup_intent(USER_ID) is True

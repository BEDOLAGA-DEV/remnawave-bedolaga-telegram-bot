"""Оплаченное продление не откатывается снимком панели, автопродление не списывает второй раз.

Случай 15.09 (подписка #3639): человек продлил вручную в 10:52, в 21:40 автопродление
списало ещё 99 ₽, а срок продлился один раз. Автопродление берёт подписки с остатком
≤3 дней по базе бота — значит, между 10:52 и 21:40 база вернулась к старой дате.
Кто мог вернуть: панель — истина, и любой её снимок (вебхук ``user.modified``,
полная сверка) переписывает срок подписки, если он расходится больше чем на минуту.
Если запись нового срока в панель не прошла (или ушла не в тот аккаунт), панель
остаётся на старой дате, и её следующий отголосок «откатывает» оплату в боте.

Здесь тот же путь боевым кодом: продление из кабинета → запись в панель падает →
вебхук панели со старой датой → автопродление. Ожидание: срок, за который
заплатили, остаётся, второго списания нет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.config import Settings, settings
from app.database.models import (
    Base,
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services.pricing_engine import RenewalPricing, pricing_engine
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TABLES = list(Base.metadata.sorted_tables)
PRICE = 9_900  # 99 ₽
BALANCE = 20_900  # 209 ₽ после пополнения 13.09
PERIOD_DAYS = 30
PANEL_ID = 555


@pytest.fixture(autouse=True)
def single_tariff_mode_quiet(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_DAYS_BEFORE', 3)
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0, raising=False)


class _PanelPatchFails:
    """Панель отвечает ошибкой на запись нового срока (как при невалидном payload или 5xx)."""

    calls: list[int] = []

    async def update_remnawave_user(self, db, subscription, **kwargs):
        type(self).calls.append(subscription.id)
        raise RuntimeError('PATCH /api/users → 400')

    async def create_remnawave_user(self, db, subscription, **kwargs):
        raise AssertionError('аккаунт в панели уже есть — create не ожидается')


def _pricing() -> RenewalPricing:
    return RenewalPricing(
        base_price=PRICE,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=PRICE,
        period_days=PERIOD_DAYS,
        is_tariff_mode=True,
        breakdown={},
    )


async def _seed(db, now: datetime) -> tuple[User, Subscription, datetime]:
    tariff = Tariff(
        name='Стандартный',
        description='',
        is_active=True,
        is_daily=False,
        period_prices={str(PERIOD_DAYS): PRICE},
        traffic_limit_gb=150,
        device_limit=3,
        allowed_squads=['s1'],
        display_order=1,
    )
    user = User(telegram_id=1, first_name='Клиент', language='ru', status='active', balance_kopeks=BALANCE)
    user.remnawave_id = PANEL_ID
    db.add_all([tariff, user])
    await db.flush()
    old_end = now + timedelta(days=2, hours=9)  # 15.09 10:52 → 17.09 21:12
    subscription = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        tariff_id=tariff.id,
        start_date=now - timedelta(days=28),
        end_date=old_end,
        traffic_limit_gb=150,
        device_limit=3,
        connected_squads=['s1'],
        remnawave_id=PANEL_ID,
        remnawave_short_id='abc',
        autopay_enabled=True,
        autopay_days_before=3,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(user)
    await db.refresh(subscription)
    return user, subscription, old_end


async def _paid_transactions(db, user_id: int) -> list[str]:
    rows = await db.execute(
        select(Transaction.description)
        .where(Transaction.user_id == user_id, Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value)
        .order_by(Transaction.id)
    )
    return [row[0] for row in rows.all()]


async def _renew_from_cabinet(db, user, subscription, monkeypatch) -> None:
    """Боевой finalize: списание, продление в базе, запись в панель (падает), очередь повтора."""
    import app.services.subscription_renewal_service as renewal_module

    monkeypatch.setattr(renewal_module, 'SubscriptionService', _PanelPatchFails)
    result = await renewal_module.SubscriptionRenewalService().finalize(
        db,
        user,
        subscription,
        _pricing(),
        description='Продление подписки на 30 дней (Стандартный)',
        payment_method=PaymentMethod.BALANCE,
    )
    assert result is not None


async def _panel_echoes_old_date(db, user, subscription, old_end: datetime) -> None:
    """Боевой обработчик ``user.modified`` с тем, что панель на самом деле хранит."""
    from app.services.remnawave_webhook_service import RemnaWaveWebhookService

    service = RemnaWaveWebhookService(bot=MagicMock())
    payload = {
        'id': PANEL_ID,
        'status': 'ACTIVE',
        'expireAt': old_end.isoformat(),
        'shortUuid': 'abc',
        'usedTrafficBytes': 0,
        'activeInternalSquads': [{'uuid': 's1'}],
    }
    await service._handle_user_modified(db, user, subscription, payload)


async def _autopay_pass(db, monkeypatch) -> None:
    """Боевой цикл автоплатежей мониторинга (без бота и без панели)."""
    from app.services.monitoring_service import MonitoringService

    monkeypatch.setattr(pricing_engine, 'calculate_renewal_price', AsyncMock(return_value=_pricing()))
    monitoring = MonitoringService(bot=None)
    monitoring.subscription_service = MagicMock(update_remnawave_user=AsyncMock(return_value=None))
    await monitoring._process_autopayments(db)


@pytest.mark.asyncio
async def test_paid_renewal_is_not_undone_by_a_stale_panel_snapshot(postgres_database, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        now = datetime.now(UTC)
        user, subscription, old_end = await _seed(db, now)
        paid_until = old_end + timedelta(days=PERIOD_DAYS)

        await _renew_from_cabinet(db, user, subscription, monkeypatch)
        await db.refresh(subscription)
        await db.refresh(user)
        assert _PanelPatchFails.calls == [subscription.id], 'запись в панель была и упала'
        assert subscription.end_date == paid_until
        assert user.balance_kopeks == BALANCE - PRICE

        await _panel_echoes_old_date(db, user, subscription, old_end)
        await db.refresh(subscription)
        assert subscription.end_date == paid_until, (
            f'снимок панели откатил оплаченный срок: {subscription.end_date.isoformat()} вместо {paid_until.isoformat()}'
        )


@pytest.mark.asyncio
async def test_autopay_does_not_charge_again_after_a_stale_panel_snapshot(postgres_database, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        now = datetime.now(UTC)
        user, subscription, old_end = await _seed(db, now)
        paid_until = old_end + timedelta(days=PERIOD_DAYS)

        await _renew_from_cabinet(db, user, subscription, monkeypatch)
        await _panel_echoes_old_date(db, user, subscription, old_end)
        # До автопродления прошло больше часа: сторож «недавно обновлено вебхуком» уже не держит.
        await db.refresh(subscription)
        subscription.last_webhook_update_at = now - timedelta(hours=2)
        await db.commit()

        await _autopay_pass(db, monkeypatch)

        await db.refresh(subscription)
        await db.refresh(user)
        paid = await _paid_transactions(db, user.id)
        assert user.balance_kopeks == BALANCE - PRICE, (
            f'списано дважды: баланс {user.balance_kopeks / 100:.0f} ₽, операции {paid}'
        )
        assert subscription.end_date == paid_until, (
            f'срок {subscription.end_date.isoformat()} вместо {paid_until.isoformat()}'
        )

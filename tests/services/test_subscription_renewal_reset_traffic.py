"""Продление подписки передаёт RESET_TRAFFIC_ON_PAYMENT в панель как есть (issue #3199).

``finalize`` гейтил панельный сброс дополнительным условием «подписка была
истёкшей», хотя CRUD ``extend_subscription`` обнуляет ``traffic_used_gb`` по
одному только ``RESET_TRAFFIC_ON_PAYMENT``. При раннем продлении (подписка ещё
активна) половины расходились: бот показывал 0, панель продолжала считать от
старого значения и возвращала потраченное ближайшим синком.

Тест узкий намеренно: ``extend_subscription`` замокан, поэтому проверяется
именно вычисление флага в ``finalize``, без переноса сюда всей машинерии CRUD.
Локальное обнуление ``traffic_used_gb`` живёт в ``app/database/crud/subscription.py``
и покрывается отдельно.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.config import settings
from app.services.subscription_renewal_service import SubscriptionRenewalPricing, SubscriptionRenewalService


def _pricing(period_days: int = 30) -> SubscriptionRenewalPricing:
    """Продление на период без промо-оффера и без доплаты за серверы."""
    return SubscriptionRenewalPricing(
        period_days=period_days,
        period_id=f'days:{period_days}',
        months=1,
        base_original_total=10000,
        discounted_total=10000,
        final_total=10000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=10000,
        server_ids=[],
        details={},
    )


async def _run_finalize(
    monkeypatch,
    *,
    status: str,
    days_left: int,
    reset_on_payment: bool,
) -> SimpleNamespace:
    """Прогоняет finalize и возвращает фейковый SubscriptionService для проверки вызовов.

    ``days_left`` < 0 означает уже истёкшую подписку.
    """
    now = datetime.now(UTC)
    subscription = SimpleNamespace(
        id=1,
        user_id=7,
        status=status,
        is_trial=False,
        start_date=now - timedelta(days=27),
        end_date=now + timedelta(days=days_left),
        tariff_id=5,
        traffic_limit_gb=100,
        traffic_used_gb=50.0,
        purchased_traffic_gb=0,
        device_limit=3,
        connected_squads=[],
        remnawave_id=42,
        updated_at=now,
    )
    # remnawave_id заполнен → finalize идёт в update_remnawave_user, а не в create
    user = SimpleNamespace(id=7, telegram_id=555, balance_kopeks=0, remnawave_id=42)

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset_on_payment, raising=False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    module = 'app.services.subscription_renewal_service'
    monkeypatch.setattr(f'{module}.extend_subscription', AsyncMock(return_value=subscription))
    # None выключает админ-уведомление в хвосте finalize
    monkeypatch.setattr(f'{module}.create_transaction', AsyncMock(return_value=None))

    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(f'{module}.SubscriptionService', lambda: panel)

    db = AsyncMock()
    # finalize перечитывает подписку под FOR UPDATE — отдаём ту же строку
    locked = Mock()
    locked.scalar_one = Mock(return_value=subscription)
    db.execute = AsyncMock(return_value=locked)

    await SubscriptionRenewalService().finalize(
        db,
        user,
        subscription,
        _pricing(),
        charge_balance_amount=0,
    )
    return panel


async def test_active_subscription_renewal_resets_traffic_in_panel(monkeypatch):
    """Issue #3199: раннее продление ещё активной подписки тоже сбрасывает трафик в панели."""
    panel = await _run_finalize(monkeypatch, status='active', days_left=3, reset_on_payment=True)

    assert panel.update_remnawave_user.await_args.kwargs.get('reset_traffic') is True


async def test_expired_subscription_renewal_still_resets_traffic_in_panel(monkeypatch):
    """Истёкшая подписка сбрасывала трафик и до фикса — это поведение не меняем."""
    panel = await _run_finalize(monkeypatch, status='expired', days_left=-1, reset_on_payment=True)

    assert panel.update_remnawave_user.await_args.kwargs.get('reset_traffic') is True


async def test_renewal_keeps_traffic_when_reset_disabled(monkeypatch):
    """С выключенным RESET_TRAFFIC_ON_PAYMENT (дефолт) сброс в панель не уходит."""
    panel = await _run_finalize(monkeypatch, status='active', days_left=3, reset_on_payment=False)

    assert panel.update_remnawave_user.await_args.kwargs.get('reset_traffic') is False

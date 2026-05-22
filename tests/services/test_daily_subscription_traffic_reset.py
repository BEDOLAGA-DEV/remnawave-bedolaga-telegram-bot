"""Регрессия на баг «вылет в лимит после истечения докупленного трафика».

Когда докупка истекает, джоба `process_traffic_resets` роняет лимит обратно к
базовому. Раньше это синкалось в панель без оглядки на `used` → на тарифах с
периодическим сбросом панели (MONTH/MONTH_ROLLING/DAY/WEEK) лимит падал посреди
цикла, used ещё высокий → панель резала активного юзера.

Фикс — выравнивание: понижение лимита откладывается до естественного сброса
панели (лимит и used обнуляются согласованно). Для NO_RESET панель used не
сбрасывает — там лимит роняется сразу, а used добивается страховкой-clamp.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.daily_subscription_service import DailySubscriptionService
from app.services.subscription_service import SubscriptionService


_GB = 1024 * 1024 * 1024


def _make_db(subscription: SimpleNamespace) -> MagicMock:
    """AsyncSession-мок: 1-й execute → подписка, 2-й → активные докупки (нет)."""
    res_sub = MagicMock()
    res_sub.scalar_one_or_none.return_value = subscription
    res_remaining = MagicMock()
    res_remaining.scalars.return_value.all.return_value = []

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[res_sub, res_remaining])
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    return db


def _make_subscription(reset_mode: str) -> SimpleNamespace:
    # base=100, докуплено 50 → текущий лимит 150
    return SimpleNamespace(
        id=1,
        user_id=1,
        tariff_id=None,
        tariff=SimpleNamespace(traffic_reset_mode=reset_mode, name='t'),
        remnawave_uuid='uuid-1',
        traffic_limit_gb=150,
        purchased_traffic_gb=50,
        traffic_reset_at=None,
        traffic_used_gb=130.0,
        updated_at=None,
    )


def _make_service(panel_used_gb: float | None) -> DailySubscriptionService:
    service = DailySubscriptionService.__new__(DailySubscriptionService)
    service._bot = None  # пропускаем уведомление
    # used из панели мокаем, чтобы не ходить в реальный API
    service._get_panel_used_gb = AsyncMock(return_value=panel_used_gb)
    return service


def _make_fake_subscription_service(panel_used_bytes: int) -> SubscriptionService:
    fake = SubscriptionService.__new__(SubscriptionService)
    fake.update_remnawave_user = AsyncMock(return_value=SimpleNamespace(used_traffic_bytes=panel_used_bytes))
    return fake


def _recently_expired() -> list[SimpleNamespace]:
    return [SimpleNamespace(traffic_gb=50, id=10, expires_at=datetime.now(UTC) - timedelta(days=1))]


async def test_defers_drop_when_panel_resets_and_user_over_limit():
    """MONTH + used 130 > нового лимита 100 → понижение ОТКЛАДЫВАЕТСЯ до сброса панели."""
    sub = _make_subscription('MONTH')
    db = _make_db(sub)
    service = _make_service(panel_used_gb=130.0)
    fake_ss = _make_fake_subscription_service(130 * _GB)

    with patch('app.services.subscription_service.SubscriptionService', return_value=fake_ss):
        await service._reset_subscription_traffic(db, sub.id, _recently_expired())

    # Лимит не тронут, докупки не удалены, в панель ничего не синкали
    assert sub.traffic_limit_gb == 150
    db.delete.assert_not_awaited()
    fake_ss.update_remnawave_user.assert_not_awaited()


async def test_applies_drop_cleanly_when_user_under_limit():
    """MONTH + used 40 <= нового лимита 100 → лимит понижается, сброса used нет."""
    sub = _make_subscription('MONTH')
    db = _make_db(sub)
    service = _make_service(panel_used_gb=40.0)
    fake_ss = _make_fake_subscription_service(40 * _GB)

    with patch('app.services.subscription_service.SubscriptionService', return_value=fake_ss):
        await service._reset_subscription_traffic(db, sub.id, _recently_expired())

    assert sub.traffic_limit_gb == 100
    db.delete.assert_awaited()
    assert fake_ss.update_remnawave_user.await_count == 1
    assert not fake_ss.update_remnawave_user.await_args_list[0].kwargs.get('reset_traffic')


async def test_no_reset_tariff_resets_used_when_over_limit():
    """NO_RESET (панель сама не сбрасывает) + used 130 > 100 → лимит вниз + сброс used (clamp)."""
    sub = _make_subscription('NO_RESET')
    db = _make_db(sub)
    service = _make_service(panel_used_gb=130.0)  # не вызовется: NO_RESET закорачивает defer
    fake_ss = _make_fake_subscription_service(130 * _GB)

    with patch('app.services.subscription_service.SubscriptionService', return_value=fake_ss):
        await service._reset_subscription_traffic(db, sub.id, _recently_expired())

    assert sub.traffic_limit_gb == 100
    assert fake_ss.update_remnawave_user.await_count == 2
    assert fake_ss.update_remnawave_user.await_args_list[1].kwargs.get('reset_traffic') is True
    assert sub.traffic_used_gb == 0.0
    # На NO_RESET панель используем как страховку, used из панели не запрашиваем
    service._get_panel_used_gb.assert_not_awaited()


async def test_forces_drop_after_grace_even_if_panel_resets():
    """MONTH, но докупка просрочена >40д → не ждём вечно: понижаем + добиваем used."""
    sub = _make_subscription('MONTH')
    db = _make_db(sub)
    service = _make_service(panel_used_gb=130.0)  # не вызовется: grace закорачивает defer
    fake_ss = _make_fake_subscription_service(130 * _GB)
    long_overdue = [SimpleNamespace(traffic_gb=50, id=10, expires_at=datetime.now(UTC) - timedelta(days=50))]

    with patch('app.services.subscription_service.SubscriptionService', return_value=fake_ss):
        await service._reset_subscription_traffic(db, sub.id, long_overdue)

    assert sub.traffic_limit_gb == 100
    assert fake_ss.update_remnawave_user.await_count == 2
    assert fake_ss.update_remnawave_user.await_args_list[1].kwargs.get('reset_traffic') is True
    service._get_panel_used_gb.assert_not_awaited()

"""Спин колеса не должен оставлять юзера LIMITED в панели (прод-репорт 27.08.2026).

Один спин с оплатой днями ходил в панель ДВАЖДЫ: сначала синк списания дней
(``_process_days_payment``), потом синк приза (``_apply_prize``). Первый PATCH
уносил ``status=ACTIVE`` со СТАРЫМ ``trafficLimitBytes`` — призовой трафик ещё не
начислен. Крон панели ``findExceededUsers`` крутится по ``*/45 * * * * *``, то есть
бьёт в :00 и :45 каждой минуты, и между двумя PATCH успевал поставить LIMITED
(``used >= limit``).

Второй PATCH это уже не чинил: ``users.service.ts::updateUser`` снимает статус
только если ``user.status !== 'ACTIVE' && dto.status === 'ACTIVE'`` либо
``user.status === 'LIMITED' && trafficLimitBytes > user.trafficLimitBytes``. PATCH #2
прочитал юзера ещё как ACTIVE (крон закоммитился в том же окне) — мимо обеих
веток, записался только лимит. Обратного пересчёта «used < limit ⇒ снять LIMITED»
в панели нет, поэтому состояние залипало навсегда: VPN не работал, лечилось
только руками.

Реальный кейс: панель id 1620, лимит 400 → 410 ГиБ, used 400.07 ГиБ,
спин 18:02:59–18:03:00 MSK.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.wheel_service import FortuneWheelService, SpinAvailability


def _subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        user_id=1,
        is_active=True,
        days_left=90,
        end_date=datetime.now(UTC) + timedelta(days=90),
        updated_at=datetime.now(UTC),
        traffic_limit_gb=400,
        status='active',
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        spin_cost_days=1,
        min_subscription_days_for_day_payment=1,
        daily_spin_limit=5,
        spin_cost_stars=10,
        spin_cost_stars_enabled=True,
        spin_cost_days_enabled=True,
    )


class _SyncSpy:
    """Пишет состояние подписки на КАЖДЫЙ поход в панель."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, db, subscription, **kwargs):
        self.calls.append(
            {
                'traffic_limit_gb': subscription.traffic_limit_gb,
                'end_date': subscription.end_date,
            }
        )
        return SimpleNamespace(status=None)


async def _run_spin(prize: SimpleNamespace, subscription: SimpleNamespace, spy: _SyncSpy) -> None:
    """Прогнать настоящий spin() с оплатой днями — контракт держится на нём.

    Проверять сами хелперы бесполезно: пропущенный flush в spin() такой тест не
    заметил бы, а именно он и превращает отложенный синк в «в панель не ушло
    ничего».
    """
    svc = FortuneWheelService()
    user = SimpleNamespace(id=1, balance_kopeks=0)
    config = _config()

    with ExitStack() as s:
        service_cls = MagicMock()
        service_cls.return_value = SimpleNamespace(update_remnawave_user=spy)
        s.enter_context(patch('app.services.wheel_service.SubscriptionService', service_cls))
        s.enter_context(patch.object(type(settings), 'is_multi_tariff_enabled', lambda self: False))
        s.enter_context(patch('app.services.wheel_service.add_user_balance', AsyncMock()))
        s.enter_context(
            patch.object(
                svc,
                'check_availability',
                AsyncMock(return_value=SpinAvailability(can_spin=True, can_pay_days=True)),
            )
        )
        s.enter_context(patch('app.services.wheel_service.get_or_create_wheel_config', AsyncMock(return_value=config)))
        s.enter_context(patch('app.services.wheel_service.get_wheel_prizes', AsyncMock(return_value=[prize])))
        s.enter_context(patch('app.database.crud.user.lock_user_for_update', AsyncMock(return_value=user)))
        s.enter_context(patch('app.services.wheel_service.get_user_spins_today', AsyncMock(return_value=0)))
        s.enter_context(
            patch('app.services.wheel_service.get_subscription_by_user_id', AsyncMock(return_value=subscription))
        )
        s.enter_context(patch.object(svc, 'calculate_prize_probabilities', lambda *a, **kw: [(prize, 100.0)]))
        s.enter_context(patch.object(svc, '_select_prize', lambda prizes_with_probs: prize))
        s.enter_context(patch.object(svc, '_calculate_rotation', lambda prizes, selected: 0))
        s.enter_context(patch('app.services.wheel_service.create_wheel_spin', AsyncMock()))

        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(fetchone=lambda: None))
        result = await svc.spin(db, user, 'subscription_days')

    assert result.success is True, f'спин не прошёл: {result.error} / {result.message}'


@pytest.mark.asyncio
async def test_days_paid_traffic_prize_syncs_panel_once_with_final_state() -> None:
    subscription = _subscription()
    original_end = subscription.end_date
    spy = _SyncSpy()
    prize = SimpleNamespace(
        id=7,
        prize_type='traffic_gb',
        prize_value=10,
        prize_value_kopeks=1000,
        display_name='+10 ГБ',
        emoji='📊',
        color='#fff',
        probability=100,
        is_active=True,
    )

    await _run_spin(prize, subscription, spy)

    assert len(spy.calls) == 1, (
        f'панель должна обновляться один раз за спин, было {len(spy.calls)}: '
        'первый PATCH со старым лимитом и есть окно для крона findExceededUsers'
    )
    call = spy.calls[0]
    assert call['traffic_limit_gb'] == 410, 'в панель должен уехать уже начисленный лимит'
    assert call['end_date'] < original_end, 'и уже списанные дни'


@pytest.mark.asyncio
async def test_days_paid_non_subscription_prize_still_syncs_charge() -> None:
    """Приз мимо подписки (баланс) — списание дней всё равно обязано доехать до панели.

    Просто убрать синк из списания мало: с призом, который подписку не трогает,
    в панель не ушло бы ничего, и юзер сохранил бы старый срок, заплатив днями.
    """
    subscription = _subscription()
    original_end = subscription.end_date
    spy = _SyncSpy()
    prize = SimpleNamespace(
        id=8,
        prize_type='balance',
        prize_value=100,
        prize_value_kopeks=10000,
        display_name='100 ₽',
        emoji='💰',
        color='#fff',
        probability=100,
        is_active=True,
    )

    await _run_spin(prize, subscription, spy)

    assert len(spy.calls) == 1, 'списание дней обязано доехать до панели даже с призом мимо подписки'
    assert spy.calls[0]['end_date'] < original_end

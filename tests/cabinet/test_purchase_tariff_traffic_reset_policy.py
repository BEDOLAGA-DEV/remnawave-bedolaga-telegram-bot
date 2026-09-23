"""Покупка тарифа из кабинета спрашивает настройки сброса трафика, а не сбрасывает всегда.

GitHub #3227: ``POST /cabinet/subscription/purchase-tariff`` синхронизировал панель
с ``reset_traffic=True`` литералом. Через этот же эндпоинт идут кнопка «Продлить
эту подписку» и карточка истёкшей подписки — трафик в панели слетал при любой
оплате, даже с выключенным ``RESET_TRAFFIC_ON_PAYMENT`` (он выключен по умолчанию).
Остальные пути продления (``/renew``, продление в боте, автопокупка, рекурренты)
настройку спрашивают.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from app.services.traffic_reset_policy import should_reset_traffic_on_tariff_purchase
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
PURCHASE_ROUTE = Path('app/cabinet/routes/subscription_modules/purchase.py')


@pytest.mark.parametrize(
    ('on_payment', 'on_switch', 'is_tariff_change', 'was_trial', 'expected'),
    [
        # Продление того же тарифа — общий выключатель оплаты.
        (False, True, False, False, False),
        (True, False, False, False, True),
        # Смена тарифа — свой выключатель.
        (True, False, True, False, False),
        (False, True, True, False, True),
        # После триала платная квота новая всегда — как в боте (``... or was_trial``).
        (False, False, False, True, True),
        (False, False, True, True, True),
    ],
)
def test_reset_follows_the_settings(monkeypatch, on_payment, on_switch, is_tariff_change, was_trial, expected):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', on_payment)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', on_switch)

    decision = should_reset_traffic_on_tariff_purchase(
        is_tariff_change=is_tariff_change, was_trial=was_trial, paid_kopeks=10_000
    )

    assert decision is expected


def test_free_tariff_change_does_not_hand_out_a_new_quota(monkeypatch):
    """Общее правило переключений: бесплатный прыжок между тарифами счётчик не обнуляет."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', True)

    assert not should_reset_traffic_on_tariff_purchase(is_tariff_change=True, was_trial=False, paid_kopeks=0)


def _purchase_tariff_panel_sync_calls() -> list[ast.Call]:
    tree = ast.parse(PURCHASE_ROUTE.read_text(encoding='utf-8'))
    route = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'purchase_tariff'
    )
    return [
        node
        for node in ast.walk(route)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'update_remnawave_user'
    ]


def test_route_does_not_hardcode_the_panel_reset():
    calls = _purchase_tariff_panel_sync_calls()
    assert calls, 'purchase_tariff больше не обновляет панель — сторож потерял цель'

    for call in calls:
        reset = next(keyword.value for keyword in call.keywords if keyword.arg == 'reset_traffic')
        assert not isinstance(reset, ast.Constant), 'reset_traffic задан константой, а не правилом'


async def _renew_same_tariff(monkeypatch, *, reset_used_traffic: bool | None) -> float:
    now = datetime.now(UTC)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
        db.add(
            Tariff(
                id=1,
                name='Тариф',
                description='',
                is_active=True,
                traffic_limit_gb=50,
                device_limit=3,
                allowed_squads=['squad-1'],
                period_prices={'30': 10_000},
                display_order=1,
            )
        )
        await db.flush()
        subscription = Subscription(
            id=10,
            remnawave_short_id='sub10',
            user_id=1,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=now - timedelta(days=20),
            end_date=now + timedelta(days=10),
            traffic_limit_gb=50,
            traffic_used_gb=12.0,
            device_limit=3,
            tariff_id=1,
            connected_squads=['squad-1'],
        )
        db.add(subscription)
        await db.commit()

        kwargs = {} if reset_used_traffic is None else {'reset_used_traffic': reset_used_traffic}
        renewed = await extend_subscription(
            db,
            subscription,
            30,
            tariff_id=1,
            traffic_limit_gb=50,
            device_limit=3,
            connected_squads=['squad-1'],
            **kwargs,
        )
        return renewed.traffic_used_gb


@pytest.mark.asyncio
async def test_bot_counter_is_kept_when_the_panel_counter_is_kept(monkeypatch):
    """Иначе бот показывает расход 0, а панель — настоящий, до следующей синхронизации."""
    assert await _renew_same_tariff(monkeypatch, reset_used_traffic=False) == 12.0


@pytest.mark.asyncio
async def test_bot_counter_is_reset_together_with_the_panel(monkeypatch):
    assert await _renew_same_tariff(monkeypatch, reset_used_traffic=True) == 0.0


@pytest.mark.asyncio
async def test_callers_that_do_not_decide_keep_the_previous_behaviour(monkeypatch):
    """Бот при покупке тарифа сбрасывает панель всегда — и счётчик в базе обнуляется, как раньше."""
    assert await _renew_same_tariff(monkeypatch, reset_used_traffic=None) == 0.0

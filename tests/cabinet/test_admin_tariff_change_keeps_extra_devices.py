"""Смена тарифа админом сохраняет докупленные устройства.

Админ меняет тариф по-разному: в кабинете одному пользователю, в кабинете
массовым действием и в админке бота. Это смена метки, а не покупка — денег не
списывается, докупленные устройства уже оплачены до конца срока. Админка бота
так и делала (лимит = база нового тарифа + докупки сверх базы старого), а оба
пути кабинета звали calc_device_limit_on_tariff_switch, которая с aec01ce0
сбрасывает лимит до базы нового тарифа: после массовой смены тарифа
пользователи теряли оплаченные устройства, хотя комментарий над вызовом
обещал «Preserve extra purchased devices». Смена тарифа самим пользователем
по-прежнему сбрасывает лимит — там считается цена перехода.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.cabinet.routes.admin_bulk_actions as bulk
from app.config import settings
from app.database.crud.subscription import (
    calc_device_limit_on_admin_tariff_change,
    calc_device_limit_on_tariff_switch,
)


@pytest.fixture(autouse=True)
def no_global_cap(monkeypatch):
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)


@pytest.mark.parametrize(
    'current, old_base, new_base, max_limit, expected',
    [
        # Докупки сверх базы старого тарифа переезжают на базу нового.
        (11, 8, 8, None, 11),
        (3, 1, 1, None, 3),
        (3, 1, 2, None, 4),
        # Докупок не было — лимит равен базе нового тарифа, в том числе ниже прежнего.
        (8, 8, 1, None, 1),
        # База старого неизвестна — переносить нечего.
        (5, None, 2, None, 2),
        # Лимит без ограничения (0 / пусто) — докупок нет.
        (0, 8, 2, None, 2),
        (None, 8, 2, None, 2),
        # Потолок тарифа.
        (11, 8, 8, 10, 10),
        # Новый тариф безлимитный (база 0) — лимит остаётся безлимитным.
        (11, 8, 0, None, 0),
        (5, 1, 0, 2, 0),
    ],
)
def test_admin_change_carries_extra_devices(current, old_base, new_base, max_limit, expected):
    assert calc_device_limit_on_admin_tariff_change(current, old_base, new_base, max_limit) == expected


def test_global_cap_applies_without_tariff_cap(monkeypatch):
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 9)

    assert calc_device_limit_on_admin_tariff_change(11, 8, 8, None) == 9


def test_user_switch_still_resets_to_new_base():
    assert calc_device_limit_on_tariff_switch(11, 8, 8, None) == 8


@pytest.mark.asyncio
async def test_bulk_change_tariff_keeps_paid_devices():
    """Массовая смена тарифа: 3 докупленных устройства сверх базы 8 остаются."""
    old_tariff = SimpleNamespace(id=1, name='Old', device_limit=8)
    new_tariff = SimpleNamespace(
        id=2, name='New', traffic_limit_gb=0, device_limit=8, max_device_limit=16, allowed_squads=[]
    )
    sub = SimpleNamespace(
        id=42,
        user_id=7,
        status='active',
        is_trial=False,
        end_date=datetime.now(UTC) + timedelta(days=30),
        tariff_id=1,
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        device_limit=11,
        daily_devices_billing_version=1,
        connected_squads=[],
        purchased_traffic_gb=0,
        traffic_reset_at=None,
    )
    db = AsyncMock()
    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False

    with (
        patch.object(bulk, 'settings', fake_settings),
        patch(
            'app.services.traffic_accounting_service.lock_accounting_subscription', AsyncMock(return_value=(sub, False))
        ),
        patch.object(bulk, 'get_tariff_by_id', AsyncMock(return_value=old_tariff)),
        patch.object(bulk, '_sync_subscription_to_panel', AsyncMock(return_value={})),
        patch('app.database.crud.transaction.create_transaction', AsyncMock(return_value=None)),
    ):
        result = await bulk._do_change_tariff(
            db,
            SimpleNamespace(id=7, username='u', subscriptions=[]),
            SimpleNamespace(tariff_id=2),
            new_tariff,
            dry_run=False,
            sub_override=sub,
        )

    assert result.success is True
    assert sub.tariff_id == 2
    assert sub.daily_devices_billing_version == 0
    assert sub.device_limit == 11


async def test_managed_same_tariff_confirmation_does_not_grant_again(monkeypatch):
    from app.database.crud import transaction as transaction_crud
    from app.services import paid_traffic_reset_service as resets, traffic_accounting_service as accounting

    tariff = SimpleNamespace(
        id=2, name='Current', traffic_limit_gb=10, device_limit=2, max_device_limit=16, allowed_squads=[]
    )
    sub = SimpleNamespace(
        id=42,
        user_id=7,
        status='active',
        is_trial=False,
        end_date=datetime.now(UTC) + timedelta(days=30),
        tariff_id=2,
        traffic_accounting_mode='managed',
        traffic_limit_gb=10,
        traffic_used_gb=9,
        device_limit=2,
        daily_devices_billing_version=1,
        purchased_traffic_gb=0,
        connected_squads=[],
        traffic_reset_at=None,
    )
    db = AsyncMock()
    grant = AsyncMock()
    transaction = AsyncMock(return_value=SimpleNamespace(id=9))
    monkeypatch.setattr(resets, 'prepare_admin_tariff_accounting', AsyncMock(return_value=(sub, object())))
    monkeypatch.setattr(accounting, 'change_managed_tariff_locked', grant)
    monkeypatch.setattr(transaction_crud, 'create_transaction', transaction)
    monkeypatch.setattr(bulk, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(bulk, '_sync_subscription_to_panel', AsyncMock())
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    result = await bulk._do_change_tariff(
        db,
        SimpleNamespace(id=7, username='u', subscriptions=[]),
        SimpleNamespace(tariff_id=2),
        tariff,
        dry_run=False,
        sub_override=sub,
    )
    assert result.success
    grant.assert_not_awaited()
    transaction.assert_not_awaited()
    assert sub.traffic_used_gb == 9


@pytest.fixture(autouse=True)
def no_cispay_in_tariff_mutation_unit_fixture(monkeypatch):
    # These fake subscriptions have no bank agreement. Real active agreements
    # are covered by test_cispay_recurrent_charges_postgres.
    monkeypatch.setattr('app.services.cispay_recurrent_guard.has_live_cispay', AsyncMock(return_value=False))
    monkeypatch.setattr('app.services.cispay_recurrent_guard.has_conflicting_cispay', AsyncMock(return_value=False))

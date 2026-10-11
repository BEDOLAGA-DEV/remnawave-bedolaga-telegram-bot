"""Мультитариф: превью смены тарифа отвечает 409, если на целевой тариф у человека
уже есть живая подписка, — так же, как сама смена. Иначе кабинет показывал цену
смены, которая после «Подтвердить» упиралась в 409.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.database.crud.subscription as subcrud
from app.cabinet.routes.subscription_modules import tariff_switch
from app.config import Settings


def _subscription(**over) -> SimpleNamespace:
    fields = {'id': 1, 'tariff_id': 1, 'is_trial': False, 'actual_status': 'active'}
    fields.update(over)
    return SimpleNamespace(**fields)


def _multi_tariff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(tariff_switch, 'resolve_subscription', AsyncMock(return_value=_subscription()))


async def _preview(tariff_id: int):
    return await tariff_switch.preview_tariff_switch(
        SimpleNamespace(tariff_id=tariff_id),
        user=SimpleNamespace(id=7),
        db=AsyncMock(),
        subscription_id=1,
    )


async def test_preview_refuses_tariff_owned_by_another_subscription(monkeypatch):
    _multi_tariff(monkeypatch)
    monkeypatch.setattr(
        subcrud,
        'get_subscription_by_user_and_tariff',
        AsyncMock(return_value=_subscription(id=2, tariff_id=5)),
    )
    tariff_lookup = AsyncMock()
    monkeypatch.setattr(tariff_switch, 'get_tariff_by_id', tariff_lookup)

    with pytest.raises(HTTPException) as exc:
        await _preview(5)

    assert exc.value.status_code == 409
    tariff_lookup.assert_not_awaited()


async def test_preview_goes_on_when_target_tariff_is_not_owned(monkeypatch):
    """Целевого тарифа у человека нет — превью идёт дальше, к тарифам и расчёту."""
    _multi_tariff(monkeypatch)
    monkeypatch.setattr(subcrud, 'get_subscription_by_user_and_tariff', AsyncMock(return_value=None))
    # Тариф не найден — 404 доказывает, что проверку владения превью прошло.
    monkeypatch.setattr(tariff_switch, 'get_tariff_by_id', AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await _preview(5)

    assert exc.value.status_code == 404

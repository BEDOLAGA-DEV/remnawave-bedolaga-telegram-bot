"""Бот не продаёт докупки старой подписке даже по старой кнопке.

Старая подписка — платная, без тарифа при включённых тарифах. Кнопок докупки
в меню у неё больше нет, но старое сообщение с «Докупить трафик» или «Изменить
устройства» могло остаться в чате. Обработчики отвечают всплывашкой «сначала
перейдите на тариф» и не открывают классический выбор с классическими ценами.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.devices as devices_module
import app.handlers.subscription.traffic as traffic_module
from app.config import Settings


def _legacy_sub() -> MagicMock:
    sub = MagicMock()
    sub.id = 1
    sub.tariff_id = None
    sub.is_trial = False
    sub.traffic_limit_gb = 100
    sub.device_limit = 3
    sub.end_date = datetime.now(UTC) + timedelta(days=10)
    return sub


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    return user


def _state() -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


@pytest.fixture(autouse=True)
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


def _assert_refused(callback: MagicMock) -> None:
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True
    assert 'тариф' in callback.answer.await_args.args[0].lower()
    callback.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_traffic_topup_is_refused_for_legacy_subscription(monkeypatch):
    sub = _legacy_sub()
    monkeypatch.setattr(traffic_module, '_resolve_subscription', AsyncMock(return_value=(sub, sub.id)))
    callback = _callback('buy_traffic')

    await traffic_module.handle_add_traffic(callback, _user(), AsyncMock(), _state())

    _assert_refused(callback)


@pytest.mark.asyncio
async def test_device_change_is_refused_for_legacy_subscription(monkeypatch):
    sub = _legacy_sub()
    monkeypatch.setattr(devices_module, '_resolve_subscription', AsyncMock(return_value=(sub, sub.id)))
    callback = _callback('subscription_change_devices')

    await devices_module.handle_change_devices(callback, _user(), AsyncMock(), _state())

    _assert_refused(callback)

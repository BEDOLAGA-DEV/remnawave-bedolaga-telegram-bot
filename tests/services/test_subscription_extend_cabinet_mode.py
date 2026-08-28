"""Кнопка «Продлить подписку» в уведомлениях при ``MAIN_MENU_MODE=cabinet``.

В мультитарифном режиме callback продления динамический — ``se:{subscription_id}``.
``CALLBACK_TO_CABINET_PATH`` статичен, такого ключа в нём нет и быть не может,
поэтому ``build_miniapp_or_callback_button`` молча проваливался в обычную
callback-кнопку: оператор включил cabinet-режим, а пользователь из уведомления
об истечении попадал в бота вместо кабинета. В одиночном режиме те же
уведомления вели в кабинет (``subscription_extend`` в маппинге есть) — то есть
поведение расходилось в зависимости от режима продаж.

Здесь закреплено: продление всегда строится через
``build_subscription_extend_button``, а в cabinet-режиме мультитарифа ведёт на
страницу продления КОНКРЕТНОЙ подписки — ``/subscriptions/{id}/renew``.
"""

from __future__ import annotations

import importlib
import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton

from app.config import settings
from app.utils.miniapp_buttons import (
    CALLBACK_TO_CABINET_PATH,
    build_subscription_extend_button,
)


CABINET_URL = 'https://cabinet.example.com'


@pytest.fixture
def cabinet_mode(monkeypatch: pytest.MonkeyPatch):
    """Полностью настроенный cabinet-режим: и режим меню, и URL кабинета."""
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', CABINET_URL, raising=False)
    return monkeypatch


def _multi_tariff(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """Метод Settings патчится на КЛАССЕ: pydantic не даёт присвоить его инстансу."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: enabled, raising=False)


# ---------------------------------------------------------------------------
# Хелпер
# ---------------------------------------------------------------------------


def test_multi_tariff_cabinet_button_opens_that_subscription_renewal(cabinet_mode) -> None:
    """РЕГРЕССИЯ: раньше здесь была callback-кнопка, уводившая в бота."""
    _multi_tariff(cabinet_mode, True)

    button = build_subscription_extend_button('💎 Продлить подписку', subscription_id=42)

    assert button.web_app is not None, (
        'В cabinet-режиме кнопка продления обязана открывать кабинет. '
        'callback_data увёл бы пользователя в бота — это и есть починенный баг.'
    )
    assert button.web_app.url == f'{CABINET_URL}/subscriptions/42/renew'
    assert button.callback_data is None


def test_single_tariff_cabinet_button_opens_cabinet(cabinet_mode) -> None:
    """Одиночный режим работал и раньше — поведение не должно поменяться."""
    _multi_tariff(cabinet_mode, False)

    button = build_subscription_extend_button('💎 Продлить подписку')

    assert button.web_app is not None
    assert button.web_app.url == f'{CABINET_URL}/subscription'


def test_multi_tariff_without_id_falls_back_to_subscription_list(cabinet_mode) -> None:
    """Без id конкретной подписки вести некуда — открываем список подписок."""
    _multi_tariff(cabinet_mode, True)

    button = build_subscription_extend_button('💎 Продлить подписку', subscription_id=None)

    assert button.web_app is not None
    assert button.web_app.url == f'{CABINET_URL}/subscription'


@pytest.mark.parametrize('multi', [True, False])
def test_bot_mode_keeps_callback_button(monkeypatch: pytest.MonkeyPatch, multi: bool) -> None:
    """Вне cabinet-режима кнопка обязана остаться обычным callback'ом."""
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', CABINET_URL, raising=False)
    _multi_tariff(monkeypatch, multi)

    button = build_subscription_extend_button('💎 Продлить подписку', subscription_id=42)

    assert button.web_app is None
    assert button.callback_data == ('se:42' if multi else 'subscription_extend')


def test_cabinet_mode_without_url_falls_back_to_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cabinet-режим без ``MINIAPP_CUSTOM_URL`` не должен ломать кнопку."""
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '', raising=False)
    _multi_tariff(monkeypatch, True)

    button = build_subscription_extend_button('💎 Продлить подписку', subscription_id=42)

    assert button.web_app is None
    assert button.callback_data == 'se:42'


def test_dynamic_callback_keeps_subscription_section_styling(cabinet_mode) -> None:
    """Стиль берётся по секции ``subscription``, а не теряется из-за ``se:{id}``.

    Иначе кнопка продления в мультитарифе выглядела бы иначе, чем та же кнопка
    в одиночном режиме — при одинаковом смысле.
    """
    _multi_tariff(cabinet_mode, True)
    multi_button = build_subscription_extend_button('💎 Продлить подписку', subscription_id=42)

    _multi_tariff(cabinet_mode, False)
    single_button = build_subscription_extend_button('💎 Продлить подписку')

    assert multi_button.style == single_button.style == 'success'


def test_dynamic_callback_is_not_added_to_static_mapping() -> None:
    """``se:{id}`` динамический — в статическом маппинге ему места нет.

    Попытка «починить» баг добавлением ключа туда бессмысленна: id подписки
    заранее неизвестен. Тест ловит такую попытку.
    """
    assert not any(key.startswith('se:') for key in CALLBACK_TO_CABINET_PATH)


# ---------------------------------------------------------------------------
# Места вызова
# ---------------------------------------------------------------------------


# Модули с уведомлениями, в которых строится кнопка продления.
EXTEND_BUTTON_MODULES = (
    'app.services.monitoring_service',
    'app.services.remnawave_webhook_service',
    'app.services.recurrent_payment_service',
)


@pytest.mark.parametrize('module_name', EXTEND_BUTTON_MODULES)
def test_call_sites_do_not_build_extend_callback_by_hand(module_name: str) -> None:
    """РЕГРЕССИЯ: каждый ручной ``f'se:{...}'`` — это ещё одна кнопка в бота.

    Ветвление «мультитариф → se:{id}, иначе subscription_extend» живёт ровно
    в одном месте — ``build_subscription_extend_button``. Скопировать его в
    уведомление и забыть про ``cabinet_path`` — исходный баг.
    """
    source = inspect.getsource(importlib.import_module(module_name))
    handmade = re.findall(r"""['"]se:""", source)
    assert not handmade, (
        f'{module_name}: callback продления собран вручную ({len(handmade)} шт.). Используйте '
        'build_subscription_extend_button — иначе в cabinet-режиме кнопка '
        'снова уведёт пользователя в бота вместо кабинета.'
    )


async def test_expired_notification_keyboard_opens_cabinet(cabinet_mode) -> None:
    """Сквозная проверка на том самом уведомлении из отчёта пользователя."""
    from app.services.monitoring_service import MonitoringService

    _multi_tariff(cabinet_mode, True)

    service = MonitoringService.__new__(MonitoringService)
    service._send_message_with_logo = AsyncMock()

    user = SimpleNamespace(telegram_id=1, language='ru', id=7)
    subscription = SimpleNamespace(id=42, tariff=None)

    sent = await service._send_subscription_expired_notification(user, subscription, tariff_name='Базовый')

    assert sent is True
    keyboard = service._send_message_with_logo.await_args.kwargs['reply_markup']
    buttons: list[InlineKeyboardButton] = [b for row in keyboard.inline_keyboard for b in row]

    assert all(b.web_app is not None for b in buttons), (
        'Все кнопки уведомления об истечении в cabinet-режиме должны вести в кабинет: '
        f'{[(b.text, b.callback_data) for b in buttons if b.web_app is None]}'
    )
    assert buttons[0].web_app.url == f'{CABINET_URL}/subscriptions/42/renew'

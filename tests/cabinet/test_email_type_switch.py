"""Выключатель писем по типу — просьба из «Багов»: чтобы некоторые письма не
отправлялись вовсе. Флаг живёт в настройке EMAIL_DISABLED_TYPES; отправители
проверяют его перед письмом; письма, без которых не войти или не получить
купленное, отключить нельзя."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_email_templates as routes
from app.cabinet.services import email_type_switch
from app.cabinet.services.email_type_switch import (
    ALWAYS_ON_EMAIL_TYPES,
    can_disable_email_type,
    is_email_type_enabled,
    set_email_type_enabled,
)
from app.config import settings


@pytest.fixture(autouse=True)
def _no_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', '', raising=False)


def test_disabled_setting_switches_types_off(monkeypatch):
    assert is_email_type_enabled('winback_trial_ending')
    monkeypatch.setattr(
        settings, 'EMAIL_DISABLED_TYPES', 'winback_trial_ending, promo_offer # комментарий', raising=False
    )
    assert not is_email_type_enabled('winback_trial_ending')
    assert not is_email_type_enabled('promo_offer')
    assert is_email_type_enabled('balance_topup')


def test_always_on_types_ignore_the_setting(monkeypatch):
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', ','.join(ALWAYS_ON_EMAIL_TYPES), raising=False)
    for notification_type in ALWAYS_ON_EMAIL_TYPES:
        assert is_email_type_enabled(notification_type), notification_type
        assert not can_disable_email_type(notification_type)
    assert not can_disable_email_type('email_layout')


@pytest.mark.asyncio
async def test_set_enabled_writes_setting_through_configuration_service(monkeypatch):
    import app.services.system_settings_service as sss

    writes = []

    async def fake_set_value(db, key, value, **kwargs):
        writes.append((key, value))
        monkeypatch.setattr(settings, key, value, raising=False)

    monkeypatch.setattr(sss.bot_configuration_service, 'set_value', fake_set_value)
    assert await set_email_type_enabled(None, 'promo_offer', False) == {'promo_offer'}
    assert await set_email_type_enabled(None, 'winback_discount', False) == {'promo_offer', 'winback_discount'}
    assert await set_email_type_enabled(None, 'promo_offer', True) == {'winback_discount'}
    assert writes == [
        ('EMAIL_DISABLED_TYPES', 'promo_offer'),
        ('EMAIL_DISABLED_TYPES', 'promo_offer,winback_discount'),
        ('EMAIL_DISABLED_TYPES', 'winback_discount'),
    ]
    assert not is_email_type_enabled('winback_discount')


@pytest.mark.asyncio
async def test_endpoint_refuses_to_disable_always_on_types(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await routes.set_template_enabled(
            'password_reset', routes.EmailTemplateEnabledRequest(enabled=False), admin=SimpleNamespace(id=1), db=None
        )
    assert exc.value.status_code == 400

    calls = []

    async def fake_set(db, notification_type, enabled):
        calls.append((notification_type, enabled))
        return {notification_type} if not enabled else set()

    monkeypatch.setattr(routes, 'set_email_type_enabled', fake_set)
    result = await routes.set_template_enabled(
        'promo_offer', routes.EmailTemplateEnabledRequest(enabled=False), admin=SimpleNamespace(id=1), db=None
    )
    assert calls == [('promo_offer', False)]
    assert result['enabled'] is False


@pytest.mark.asyncio
async def test_promo_email_is_skipped_when_disabled(monkeypatch):
    import importlib

    import app.services.promo_offer_email as promo

    email_service_module = importlib.import_module('app.cabinet.services.email_service')
    email_service = MagicMock()
    email_service.is_configured = MagicMock(return_value=True)
    monkeypatch.setattr(email_service_module, 'email_service', email_service)
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', 'promo_offer', raising=False)

    ok = await promo.send_promo_offer_email(email='u@example.com', language='ru', message_text='hi', valid_hours=24)
    assert ok is False
    email_service.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_receipt_email_is_skipped_when_disabled(monkeypatch):
    import importlib

    from app.services.nalogo_service import _send_receipt_email

    email_service_module = importlib.import_module('app.cabinet.services.email_service')
    send_mock = MagicMock(return_value=True)
    monkeypatch.setattr(email_service_module.email_service, 'send_email', send_mock)
    monkeypatch.setattr(email_service_module.email_service, 'is_configured', lambda: True)
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', 'nalogo_receipt', raising=False)

    assert await _send_receipt_email('a@b.c', '100 ₽', 'https://x/print', None) is False
    send_mock.assert_not_called()


def test_switch_module_is_free_of_side_effects_on_import():
    assert email_type_switch.EMAIL_DISABLED_TYPES_KEY == 'EMAIL_DISABLED_TYPES'
    _ = AsyncMock


@pytest.mark.asyncio
async def test_delivery_service_skips_disabled_type(monkeypatch):
    """Основной путь уведомлений: отключённый тип не уходит, включённый — уходит."""
    from app.services.notification_delivery_service import NotificationType, notification_delivery_service as service

    captured: list[dict] = []

    def fake_send(**kwargs):
        captured.append(kwargs)
        return True

    monkeypatch.setattr(service.email_service, 'is_configured', lambda: True)
    monkeypatch.setattr(service.email_service, 'send_email', fake_send)
    import importlib

    overrides_module = importlib.import_module('app.cabinet.services.email_template_overrides')
    monkeypatch.setattr(overrides_module, 'get_rendered_override', AsyncMock(return_value=None))
    user = SimpleNamespace(
        id=1,
        email='a@b.c',
        email_verified=True,
        language='ru',
        first_name='A',
        username='a',
        telegram_id=None,
        notification_settings={},
    )
    monkeypatch.setattr(settings, 'EMAIL_DISABLED_TYPES', 'balance_topup', raising=False)

    assert await service._send_email_notification(user, NotificationType.BALANCE_TOPUP, {}) is False
    assert captured == []
    assert await service._send_email_notification(user, NotificationType.SUBSCRIPTION_EXPIRED, {}) is True
    assert len(captured) == 1

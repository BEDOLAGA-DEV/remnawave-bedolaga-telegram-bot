from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_protocols_disabled_by_default():
    # New feature ships OFF; admin enables it at runtime via bot settings.
    assert settings.is_protocols_enabled() is False


def test_settings_keyboard_hides_button_when_disabled(monkeypatch):
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: False, raising=False)
    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=None, subscription=None)
    assert 'nz!_subscription_protocols' not in _cbs(kb)


def test_settings_keyboard_shows_button_when_enabled(monkeypatch):
    from app.keyboards.inline import get_updated_subscription_settings_keyboard

    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: True, raising=False)
    kb = get_updated_subscription_settings_keyboard('ru', True, tariff=None, subscription=None)
    assert 'nz!_subscription_protocols' in _cbs(kb)


def test_detail_keyboard_hides_button_when_disabled(monkeypatch):
    monkeypatch.setattr(
        'app.services.freeze_settings_service.FreezeSettingsService.is_enabled',
        staticmethod(lambda: False),
        raising=False,
    )
    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: False, raising=False)
    from app.handlers.subscription.my_subscriptions import _build_subscription_detail_keyboard

    sub = SimpleNamespace(actual_status='active', frozen_at=None)
    kb = _build_subscription_detail_keyboard(7, sub=sub)
    assert 'nz!_subscription_protocols' not in _cbs(kb)


@pytest.mark.asyncio
async def test_handler_refuses_and_short_circuits_when_disabled(monkeypatch):
    import app.handlers.subscription.common as common
    import app.handlers.subscription.protocols as protocols

    monkeypatch.setattr(type(settings), 'is_protocols_enabled', lambda self: False, raising=False)

    resolved = {'called': False}

    async def fake_resolve(*a, **k):
        resolved['called'] = True
        return SimpleNamespace(connected_squads=[]), 5

    monkeypatch.setattr(common, 'resolve_subscription_from_context', fake_resolve)

    cb = SimpleNamespace(
        data='nz!_subscription_protocols',
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(id=1, language='ru', promo_group_id=None)

    await protocols.handle_manage_protocols(cb, db_user, db=None, state=SimpleNamespace())

    cb.answer.assert_awaited()  # alert shown
    cb.message.edit_text.assert_not_awaited()  # screen not rendered
    assert resolved['called'] is False  # short-circuits before resolving the subscription

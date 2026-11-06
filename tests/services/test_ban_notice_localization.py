"""Built-in ban messages follow the recipient locale; operator templates remain intact."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from app.localization.texts import get_texts
from app.services import ban_notification_service as module


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']
KINDS = [
    'punishment',
    'revoke',
    'enabled',
    'wifi',
    'mobile',
    'torrent',
    'hwid_limit',
    'suspicious_destination',
    'traffic_limit',
    'manual',
    'warning',
]


async def _invoke(service, kind, reason=None):
    common = dict(db=object(), user_identifier='masked-1', username='Example')
    if kind in {'punishment', 'revoke'}:
        return await service.send_punishment_notification(
            **common, ip_count=7, limit=3, ban_minutes=15, node_name='<node>', revoke=kind == 'revoke'
        )
    if kind == 'enabled':
        return await service.send_enabled_notification(**common)
    if kind == 'warning':
        return await service.send_warning_notification(**common, warning_message=reason or '<Оператор>')
    if kind in {'wifi', 'mobile'}:
        return await getattr(service, f'send_network_{kind}_notification')(
            **common, ban_minutes=15, network_type='<network>', node_name='<node>'
        )
    return await service.send_typed_ban_notification(
        **common, notification_type=kind, ban_minutes=15, reason=reason, node_name='<node>'
    )


def _setup(monkeypatch, language, *, email=False):
    monkeypatch.setattr(Settings, 'is_notifications_enabled', lambda self: True)
    for kind in KINDS:
        name = f'BAN_MSG_{kind.upper()}'
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)
    service = module.BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=7, telegram_id=None if email else 123, language=language)
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=user))
    return service


def _assert_language(text, language):
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text.replace('Оператор', '')) is None, text
    assert 'BAN_DEFAULT_' not in text
    assert 'BAN_NOTICE_' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('kind', KINDS)
async def test_every_builtin_ban_template_uses_recipient_language(monkeypatch, language, kind):
    service = _setup(monkeypatch, language)
    result = await _invoke(service, kind)
    expected_status = 'Предупреждение отправлено' if kind == 'warning' else 'Уведомление отправлено'
    assert result == (True, expected_status, 123)  # Internal/API result remains compatible.
    call = service._bot.send_message.await_args.kwargs
    _assert_language(call['text'], language)
    if kind not in {'enabled', 'warning'}:
        assert '&lt;node&gt;' in call['text']
    if kind in {'wifi', 'mobile'}:
        assert '&lt;network&gt;' in call['text']
    if kind == 'warning':
        assert '&lt;Оператор&gt;' in call['text']
    button = call['reply_markup'].inline_keyboard[0][0]
    assert button.callback_data == 'ban_notify:delete'
    _assert_language(button.text, language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('kind', KINDS)
async def test_operator_template_is_not_translated(monkeypatch, language, kind):
    service = _setup(monkeypatch, language)
    custom = 'Оператор: custom &amp; text'
    monkeypatch.setattr(settings, f'BAN_MSG_{kind.upper()}', custom)
    await _invoke(service, kind)
    assert service._bot.send_message.await_args.kwargs['text'] == custom


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_invalid_custom_template_keeps_existing_fallback_behavior_localized(monkeypatch, language):
    service = _setup(monkeypatch, language)
    monkeypatch.setattr(settings, 'BAN_MSG_MANUAL', 'custom {unknown_placeholder}')
    await _invoke(service, 'manual', '<Оператор>')
    text = service._bot.send_message.await_args.kwargs['text']
    assert '&lt;Оператор&gt;' in text
    assert '&lt;node&gt;' in text
    assert '15' in text
    _assert_language(text, language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('kind', ['punishment', 'revoke', 'wifi', 'mobile', 'manual'])
async def test_email_reason_localizes_only_builtin_fragments(monkeypatch, language, kind):
    service = _setup(monkeypatch, language, email=True)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(module.notification_delivery_service, 'notify_ban', notify)
    result = await _invoke(service, kind, 'Оператор' if kind == 'manual' else None)
    assert result == (True, 'Email уведомление отправлено', None)
    reason = notify.await_args.kwargs['reason']
    assert '<node>' in reason  # Email delivery still owns escaping.
    if kind == 'manual':
        assert 'Оператор' in reason
    _assert_language(reason, language)
    service._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ban_user_without_language_uses_configured_default(monkeypatch):
    service = _setup(monkeypatch, 'ru')
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'fa')
    service._find_user_by_identifier.return_value = SimpleNamespace(id=7, telegram_id=123)
    await _invoke(service, 'enabled')
    assert service._bot.send_message.await_args.kwargs['text'] == get_texts('fa').t('BAN_DEFAULT_ENABLED')


def test_template_setting_names_cover_exact_configuration_defaults():
    tree = ast.parse(Path(module.__file__).read_text())
    names = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.startswith('BAN_MSG_')
        and n.value != 'BAN_MSG_'
    }
    assert names == {f'BAN_MSG_{kind.upper()}' for kind in KINDS}

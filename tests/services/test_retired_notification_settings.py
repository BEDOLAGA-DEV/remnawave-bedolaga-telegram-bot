"""Unused notification knobs must not remain editable or silently apply old values."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.localization.texts import get_texts
from app.services import system_settings_service as module


RETIRED = (
    'NOTIFICATION_RETRY_ATTEMPTS',
    'NOTIFICATION_CACHE_HOURS',
    'MONITORING_LOGS_RETENTION_DAYS',
    'REFERRAL_NOTIFICATION_RETRY_ATTEMPTS',
)


@pytest.mark.parametrize('key', RETIRED)
def test_retired_notification_setting_is_not_editable(key):
    assert key not in Settings.model_fields
    with pytest.raises(KeyError):
        module.bot_configuration_service.get_definition(key)
    assert key in module.bot_configuration_service._RETIRED_SETTINGS


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_referral_settings_no_longer_promises_unused_retry_count(language):
    text = get_texts(language).t('ADMIN_REFERRALS_UI_001')
    assert '{v5}' not in text
    assert '{v4}' in text  # Actual enable/disable control is retained.


async def test_old_notification_settings_warn_without_being_applied(monkeypatch):
    service = module.bot_configuration_service
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(key=key, value='7') for key in RETIRED]
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=SimpleNamespace(execute=AsyncMock(return_value=result)))
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(module, 'AsyncSessionLocal', lambda: session)
    logger = MagicMock()
    monkeypatch.setattr(module, 'logger', logger)
    apply = MagicMock()
    monkeypatch.setattr(service, '_apply_to_settings', apply)
    for key in RETIRED:
        monkeypatch.setenv(key, '7')
    await service.initialize(sync_web_api_token=False)
    apply.assert_not_called()
    warnings = logger.warning.call_args_list
    for key in RETIRED:
        assert sum(call.kwargs.get('key') == key for call in warnings) == 2


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_referral_settings_screen_still_renders(monkeypatch, language):
    import inspect

    from app.handlers.admin import referrals

    monkeypatch.setattr(type(referrals.settings), 'is_referral_levels_scheme', lambda self: False)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(referrals.show_referral_settings)(callback, SimpleNamespace(language=language), None)
    text = callback.message.edit_text.await_args.args[0]
    assert '{v' not in text
    callback.answer.assert_awaited_once_with()

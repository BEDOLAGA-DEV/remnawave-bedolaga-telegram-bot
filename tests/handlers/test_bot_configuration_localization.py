"""Admin settings UI selects the requesting admin's language without changing data."""

import pytest

from app.handlers.admin import bot_configuration as ui
from app.services.system_settings_service import bot_configuration_service as service


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_setting_metadata_uses_language(monkeypatch, language):
    seen = []
    original = service.get_setting_guidance

    def guidance(key, language=None):
        seen.append(language)
        return original(key, language)

    monkeypatch.setattr(service, 'get_setting_guidance', guidance)
    ui._render_setting_text('DEFAULT_LANGUAGE', language=language)
    assert seen == [language]


def test_localized_keyboards_keep_callback_contract():
    def callbacks(language):
        keyboard = ui._build_groups_keyboard(language=language)
        return [[button.callback_data for button in row] for row in keyboard.inline_keyboard]

    assert callbacks('en') == callbacks('ru') == callbacks('fa')


def test_localized_metadata_does_not_mutate_shared_definitions():
    before = dict(ui.CATEGORY_GROUP_METADATA['core'])
    ui._get_group_meta('core', language='fa')
    assert ui.CATEGORY_GROUP_METADATA['core'] == before


@pytest.mark.parametrize('language', ['en', 'zh', 'fa'])
def test_settings_screens_have_localized_static_copy(language):
    import re

    keyboard = ui._build_groups_keyboard(language=language)
    screens = [ui._render_dashboard_overview(language=language)]
    screens.extend('\n'.join(ui._format_preset_preview(key, language=language)[1]) for key in ui.PRESET_CONFIGS)
    screens.append(ui._render_setting_text('DEFAULT_LANGUAGE', language=language))
    screens.extend(button.text for row in keyboard.inline_keyboard for button in row)
    assert not re.search('[А-Яа-яЁё]', '\n'.join(screens))


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_summary_keeps_secret_masking_and_operator_values(monkeypatch, language):
    monkeypatch.setattr(service, 'get_current_value', lambda key: 'masked-example-token')
    monkeypatch.setattr(service, 'get_original_value', lambda key: 'masked-example-token')
    summary = service.get_setting_summary('YOOKASSA_SECRET_KEY', language=language)
    assert summary['current'] == summary['original'] == service.SECRET_MASK
    operator_value = 'Operator {custom} label'
    monkeypatch.setattr(service, 'get_current_value', lambda key: operator_value)
    assert service.get_setting_summary('SUPPORT_USERNAME', language=language)['current'] == operator_value


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_localized_parse_errors_preserve_valid_values(language):
    import re

    assert service.parse_user_value('TRIAL_DURATION_DAYS', '21', language=language) == 21
    assert service.parse_user_value('ENABLE_NOTIFICATIONS', 'false', language=language) is False
    for key, value in [('TRIAL_DURATION_DAYS', 'oops'), ('ENABLE_NOTIFICATIONS', 'oops'), ('SALES_MODE', 'oops')]:
        with pytest.raises(ValueError) as raised:
            service.parse_user_value(key, value, language=language)
        if language in {'en', 'zh', 'fa'}:
            assert not re.search('[А-Яа-яЁё]', str(raised.value))


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
async def test_menu_uses_account_language(monkeypatch, language):
    import inspect
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    seen = []
    original = ui._render_dashboard_overview

    def overview(language='ru'):
        seen.append(language)
        return original(language)

    monkeypatch.setattr(ui, '_render_dashboard_overview', overview)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await inspect.unwrap(ui.show_bot_config_menu)(callback, SimpleNamespace(language=language), None, state)
    assert seen == [language]
    callback.message.edit_text.assert_awaited_once()

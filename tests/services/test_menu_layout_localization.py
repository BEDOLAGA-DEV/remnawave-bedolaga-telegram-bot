from string import Formatter

import pytest

from app.config import settings
from app.keyboards.inline import get_reset_traffic_confirm_keyboard
from app.services.menu_layout.constants import AVAILABLE_CALLBACKS, BUILTIN_BUTTONS_INFO, DEFAULT_MENU_CONFIG
from app.services.menu_layout.service import MenuLayoutService


LABELS = (
    [button['text'] for button in DEFAULT_MENU_CONFIG['buttons'].values()]
    + [button['default_text'] for button in BUILTIN_BUTTONS_INFO]
    + [button['text'] for button in AVAILABLE_CALLBACKS]
)


@pytest.mark.parametrize('labels', LABELS)
def test_menu_owned_defaults_have_all_languages_and_same_placeholders(labels):
    assert set(labels) == {'ru', 'en', 'ua', 'zh', 'fa'}
    expected = {field for _, field, _, _ in Formatter().parse(labels['en']) if field}
    for value in labels.values():
        assert {field for _, field, _, _ in Formatter().parse(value) if field} == expected


@pytest.mark.parametrize('language', ['ua', 'zh', 'fa'])
def test_legacy_default_menu_labels_are_localized_without_mutating_config(language):
    labels = DEFAULT_MENU_CONFIG['buttons']['balance']['text']
    stored = {key: labels[key] for key in ('ru', 'en')}
    before = dict(stored)
    assert MenuLayoutService._get_localized_text(stored, language) == labels[language]
    assert stored == before


def test_custom_menu_label_is_preserved_when_requested_translation_is_missing():
    custom = {'ru': 'Моя кнопка', 'en': 'Operator button'}
    assert MenuLayoutService._get_localized_text(custom, 'fa') == 'Operator button'
    assert MenuLayoutService._get_localized_text({**custom, 'fa': 'CUSTOM'}, 'fa') == 'CUSTOM'


@pytest.mark.parametrize('language, word', [('en', 'Reset'), ('zh', '重置'), ('fa', 'بازنشانی')])
def test_traffic_reset_confirmation_localizes_label_without_changing_price(monkeypatch, language, word):
    monkeypatch.setattr(type(settings), 'is_traffic_topup_blocked', lambda self: False)
    keyboard = get_reset_traffic_confirm_keyboard(12345, language)
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == 'confirm_reset_traffic'
    assert word in button.text
    assert settings.format_price(12345) in button.text

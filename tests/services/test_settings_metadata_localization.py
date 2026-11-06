"""Metadata changes language without changing setting values or shared choice caches."""

from types import SimpleNamespace

import pytest

from app.cabinet.routes import admin_settings
from app.services import system_settings_service
from app.services.system_settings_service import bot_configuration_service as service


class MetadataTexts:
    def __init__(self, language):
        self.language = language

    def t(self, key, fallback=''):
        return f'{self.language}:{key}'


@pytest.fixture
def translated_metadata(monkeypatch):
    monkeypatch.setattr(system_settings_service, 'get_texts', MetadataTexts, raising=False)


def test_static_hints_are_localized_without_mutating_source(translated_metadata):
    source = dict(service.SETTING_HINTS['CISPAY_RECURRENT_ENABLED'])
    hints = service.get_setting_guidance('CISPAY_RECURRENT_ENABLED', language='fa')
    assert hints['description'] == 'fa:SETTINGS_HINT_CISPAY_RECURRENT_ENABLED_DESCRIPTION'
    assert hints['example'] == 'false'
    assert service.SETTING_HINTS['CISPAY_RECURRENT_ENABLED'] == source


def test_choice_values_and_shared_labels_do_not_change(translated_metadata):
    original = list(service.get_choice_options('SALES_MODE'))
    translated = service.get_choice_options('SALES_MODE', language='en')
    assert [o.value for o in translated] == [o.value for o in original]
    assert all(o.label.startswith('en:SETTINGS_CHOICE_') for o in translated)
    assert service.get_choice_options('SALES_MODE') == original
    assert all(a is not b for a, b in zip(original, translated, strict=True))


def test_serialization_keeps_secrets_masked_and_uses_requested_metadata_language(monkeypatch, translated_metadata):
    monkeypatch.setattr(system_settings_service.settings, 'YOOKASSA_SECRET_KEY', 'masked-fixture-secret')
    definition = service.get_definition('YOOKASSA_SECRET_KEY')
    result = admin_settings._serialize_definition(definition, language='zh')
    assert result.current == service.SECRET_MASK
    assert result.is_secret is True
    assert result.hint.description.startswith('zh:SETTINGS_')
    assert result.key == definition.key
    assert 'masked-fixture-secret' not in result.model_dump_json()


@pytest.mark.asyncio
async def test_requested_language_overrides_saved_admin_language(translated_metadata):
    result = await admin_settings.get_setting(
        'CISPAY_RECURRENT_ENABLED', admin=SimpleNamespace(language='ru'), language='fa-IR'
    )
    assert result.hint.description.startswith('fa:')


@pytest.mark.parametrize(
    ('requested', 'fallback', 'expected'),
    [
        ('en-US', 'ru', 'en'),
        ('zh-Hans', 'ru', 'zh'),
        ('fa_IR', 'ru', 'fa'),
        ('uk-UA', 'ru', 'ua'),
        ('../../en', 'fa', 'fa'),
        ('en;q=1', 'zh', 'zh'),
        (None, 'en', 'en'),
        ('de', 'ua', 'ua'),
    ],
)
def test_metadata_language_is_allowlisted(requested, fallback, expected):
    assert system_settings_service.normalize_settings_language(requested, fallback) == expected


@pytest.mark.parametrize(
    'key', ['SIMPLE_SUBSCRIPTION_PERIOD_DAYS', 'SIMPLE_SUBSCRIPTION_DEVICE_LIMIT', 'SIMPLE_SUBSCRIPTION_TRAFFIC_GB']
)
def test_dynamic_choices_keep_values_and_do_not_contaminate_cache(key, translated_metadata):
    original = service.get_choice_options(key)
    original_labels = [o.label for o in original]
    translated = service.get_choice_options(key, language='fa')
    assert [o.value for o in translated] == [o.value for o in original]
    assert [o.label for o in service.CHOICES[key]] == original_labels
    assert all('fa:SETTINGS_' in o.label for o in translated)


def test_operator_content_is_not_translated(monkeypatch, translated_metadata):
    originals = dict(service._original_values)
    originals['GRACE_ACCESS_ALLOWED_SERVICES'] = 'Custom service {operator_value}'
    monkeypatch.setattr(service, '_original_values', originals)
    hints = service.get_setting_guidance('GRACE_ACCESS_ALLOWED_SERVICES', language='fa')
    # Static example is translated, but original values formatted for display remain data.
    assert hints['example'].startswith('fa:SETTINGS_HINT_')
    assert (
        service.format_value_human('GRACE_ACCESS_ALLOWED_SERVICES', originals['GRACE_ACCESS_ALLOWED_SERVICES'], 'fa')
        == originals['GRACE_ACCESS_ALLOWED_SERVICES']
    )


@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
def test_real_metadata_translations_cover_static_hints_categories_and_choices(language):
    import re

    from app.localization.texts import get_texts

    texts = get_texts(language)
    service.initialize_definitions()
    for key, source in service.SETTING_HINTS.items():
        if key not in service._definitions:
            continue
        guidance = service.get_setting_guidance(key, language)
        for field, value in source.items():
            if value and re.search('[А-Яа-я]', value):
                translated = texts.t(f'SETTINGS_HINT_{key}_{field.upper()}', '')
                assert translated, (language, key, field)
                assert guidance[field], (language, key, field)
    for key in service.CATEGORY_DESCRIPTIONS:
        assert texts.t(f'SETTINGS_CATEGORY_{key}_DESCRIPTION', ''), (language, key)
        assert service.get_category_description(key, language)
    for key in service.CHOICES:
        original = service.get_choice_options(key)
        translated = service.get_choice_options(key, language)
        assert [o.value for o in original] == [o.value for o in translated], (language, key)
        assert all(o.label for o in translated)


def test_brand_substitution_does_not_interpret_other_template_placeholders(monkeypatch):
    monkeypatch.setattr(
        type(system_settings_service.settings), 'get_mulenpay_display_name', lambda _self: 'Custom {telegram_id}'
    )
    hint = service.get_setting_guidance('PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED', 'en')
    assert 'Custom {telegram_id}' in hint['warning']
    template_hint = service.get_setting_guidance('REMNAWAVE_USER_DESCRIPTION_TEMPLATE', 'en')
    assert '{telegram_id}' in template_hint['format']
    assert '{full_name}' in template_hint['format']

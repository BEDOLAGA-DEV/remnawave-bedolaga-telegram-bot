"""Locale names cannot select files outside the configured locale directories."""

import json

import pytest

from app.localization import loader


@pytest.mark.parametrize('extension', ['.json', '.yml', '.yaml'])
@pytest.mark.parametrize('language_kind', ['relative', 'absolute', 'symlink'])
def test_locale_cannot_read_external_file(tmp_path, monkeypatch, extension, language_kind):
    locales = tmp_path / 'locales'
    locales.mkdir()
    external = tmp_path / f'masked{extension}'
    external.write_text(json.dumps({'MASKED': 'outside locale directory'}))
    if language_kind == 'relative':
        language = '../masked'
    elif language_kind == 'absolute':
        language = str(tmp_path / 'masked')
    else:
        (locales / f'masked{extension}').symlink_to(external)
        language = 'masked'
    monkeypatch.setattr(loader, '_DEFAULT_LOCALES_DIR', locales)
    monkeypatch.setattr(loader.settings, 'LOCALES_PATH', str(locales))
    assert loader._load_default_locale(language) == {}
    assert loader._load_user_locale(language) == {}
    assert not loader._locale_file_exists(language)


@pytest.mark.parametrize('extension', ['.json', '.yml', '.yaml'])
@pytest.mark.parametrize('language', ['ru', 'pt-BR', 'zh_Hans'])
def test_custom_locale_names_remain_supported(tmp_path, monkeypatch, extension, language):
    (tmp_path / f'{language}{extension}').write_text(json.dumps({'MASKED': 'custom translation'}))
    monkeypatch.setattr(loader.settings, 'LOCALES_PATH', str(tmp_path))
    assert loader._load_user_locale(language) == {'MASKED': 'custom translation'}


def test_invalid_language_uses_default_locale(tmp_path, monkeypatch):
    (tmp_path / 'ru.json').write_text(json.dumps({'MASKED': 'fallback translation'}))
    monkeypatch.setattr(loader, '_DEFAULT_LOCALES_DIR', tmp_path)
    monkeypatch.setattr(loader, 'DEFAULT_LANGUAGE', 'ru')
    monkeypatch.setattr(loader.settings, 'LOCALES_PATH', str(tmp_path))
    loader.clear_locale_cache()
    try:
        assert loader.load_locale('../masked') == {'MASKED': 'fallback translation'}
    finally:
        loader.clear_locale_cache()

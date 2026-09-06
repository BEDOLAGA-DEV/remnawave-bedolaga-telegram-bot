"""Тексты премиум-трафика должны быть во всех языках и с теми же подстановками.

Пропущенный ключ виден не сразу: `texts.t(key, default)` молча отдаёт запасной
русский текст, и пользователь с другим языком получает уведомление не на своём.
Лишняя или переименованная подстановка — хуже: `.format` бросит KeyError уже в
момент отправки, то есть в проде и только для одного языка.
"""

from __future__ import annotations

import json
import pathlib
import re


LOCALES = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'

PREMIUM_KEYS = (
    'PREMIUM_TRAFFIC_LABEL',
    'PREMIUM_TRAFFIC_WARNING',
    'PREMIUM_TRAFFIC_EXHAUSTED',
    'PREMIUM_TRAFFIC_RESTORED',
)

PLACEHOLDER = re.compile(r'{(\w+)}')


def _load() -> dict[str, dict[str, str]]:
    return {path.stem: json.loads(path.read_text(encoding='utf-8')) for path in sorted(LOCALES.glob('*.json'))}


def test_all_languages_are_present():
    """Пять языков заявлены в проекте — значит переводы нужны на все пять."""
    assert set(_load()) == {'ru', 'en', 'ua', 'zh', 'fa'}


def test_premium_keys_exist_in_every_language():
    missing = {lang: [key for key in PREMIUM_KEYS if key not in texts] for lang, texts in _load().items()}
    missing = {lang: keys for lang, keys in missing.items() if keys}

    assert not missing, f'нет переводов премиум-трафика: {missing}'


def test_placeholders_match_across_languages():
    """Разошедшиеся подстановки уронят отправку KeyError только на одном языке."""
    locales = _load()
    for key in PREMIUM_KEYS:
        reference = PLACEHOLDER.findall(locales['ru'][key])
        for lang, texts in locales.items():
            assert PLACEHOLDER.findall(texts[key]) == reference, (
                f'{lang}.{key}: подстановки {PLACEHOLDER.findall(texts[key])} не совпадают с русскими {reference}'
            )


def test_translations_are_not_copies_of_russian():
    """Забытый перевод легко пропустить — он «работает», но не на том языке."""
    locales = _load()
    for lang, texts in locales.items():
        if lang == 'ru':
            continue
        for key in PREMIUM_KEYS:
            assert texts[key] != locales['ru'][key], f'{lang}.{key} — копия русского текста'


def test_key_sets_are_identical_in_all_languages():
    """Общий инвариант файлов локализации, а не только премиум-ключей."""
    locales = _load()
    reference = set(locales['ru'])
    for lang, texts in locales.items():
        assert set(texts) == reference, (
            f'{lang}: нет {sorted(reference - set(texts))[:5]}, лишние {sorted(set(texts) - reference)[:5]}'
        )

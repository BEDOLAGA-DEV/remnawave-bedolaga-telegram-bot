"""Новые настройки должны попадать в категорию, где их будут искать.

Категория выводится из ПЕРВОГО слова ключа, поэтому без явной привязки настройка
уезжает в автокатегорию по глаголу: ALLOW_DEVICES_BELOW_TARIFF_LIMIT оказывался
в «ALLOW», где его не найдёт ни один оператор.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.system_settings_service import bot_configuration_service as service


@pytest.mark.parametrize(
    ('key', 'expected_category'),
    [
        # Ищут рядом с MAX_DEVICES_LIMIT и PRICE_PER_DEVICE, а не в «ALLOW»
        ('ALLOW_DEVICES_BELOW_TARIFF_LIMIT', 'SUBSCRIPTIONS_CORE'),
        ('MAX_DEVICES_LIMIT', 'SUBSCRIPTIONS_CORE'),
        ('CABINET_REQUIRE_LEGAL_CONSENT', 'CABINET'),
        ('CABINET_LEGAL_CONSENT_PRECHECKED', 'CABINET'),
        ('WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS', 'WEB_API'),
    ],
)
def test_setting_lands_in_expected_category(key: str, expected_category: str) -> None:
    service.initialize_definitions()

    definition = service._definitions.get(key)

    assert definition is not None, f'{key} не попал в настройки админки'
    assert definition.category_key == expected_category


def test_no_setting_falls_into_a_single_verb_category() -> None:
    """Категория из одного глагола — признак забытой привязки в CATEGORY_KEY_OVERRIDES.

    Проверяем только НАШИ префиксы-глаголы: они означают, что ключ начинается с
    действия, а не с подсистемы, и категория получилась бессмысленной.
    """
    service.initialize_definitions()

    verb_categories = {'ALLOW', 'DISABLE', 'ENABLE', 'ACTIVATE', 'RESET', 'LOW', 'BUY'}
    stray = {
        key: definition.category_key
        for key, definition in service._definitions.items()
        if definition.category_key in verb_categories
    }

    assert not stray, f'Настройки без осмысленной категории: {stray}'


def test_every_setting_is_exposed_unless_explicitly_excluded() -> None:
    """Ни одна настройка не должна пропасть из админки молча."""
    service.initialize_definitions()

    missing = {
        key for key in Settings.model_fields if key not in service.EXCLUDED_KEYS and key not in service._definitions
    }

    assert not missing, f'Настройки не видны в админке: {sorted(missing)}'

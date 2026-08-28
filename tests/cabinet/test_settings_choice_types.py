"""Настройка со списком вариантов должна сохраняться, а не отвечать 400 на всё.

Варианты (``ChoiceOption.value``) всегда описаны СТРОКАМИ, а присланное значение
проверка приводит к типу настройки. У булевой это ``True``/``False``, и прямое
сравнение с ``'true'`` не совпадало никогда: PUT возвращал 400 на любое значение,
включая перечисленные в самих вариантах, — настройка становилась несохраняемой.

Проверка идёт по всем настройкам со списком, а не только по сломавшимся: то же
самое повторится с первой же числовой настройкой, которой добавят варианты.
"""

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_settings import _coerce_value
from app.services.system_settings_service import bot_configuration_service


def _keys_with_choices() -> list[str]:
    bot_configuration_service.initialize_definitions()
    return sorted(key for key in bot_configuration_service.CHOICES if bot_configuration_service.get_definition(key))


def test_every_listed_option_is_accepted():
    """Вариант, показанный админу, обязан сохраняться.

    Иначе интерфейс предлагает выбор, который сервер отвергает.
    """
    rejected: list[str] = []
    for key in _keys_with_choices():
        for option in bot_configuration_service.get_choice_options(key):
            try:
                _coerce_value(key, option.value)
            except HTTPException as exc:
                rejected.append(f'{key}={option.value!r}: {exc.status_code} {exc.detail}')

    assert rejected == [], 'варианты из списка отвергаются сервером:\n' + '\n'.join(rejected)


@pytest.mark.parametrize('key', ['REFERRAL_ALLOW_DAYS_TARGET_CHOICE', 'REFERRAL_ALLOW_REWARD_KIND_CHOICE'])
@pytest.mark.parametrize(('sent', 'expected'), [(True, True), (False, False), ('true', True), ('false', False)])
def test_boolean_setting_accepts_both_shapes(key, sent, expected):
    """Кабинет шлёт настоящий bool, бот — строку. Принимать надо обе формы."""
    assert _coerce_value(key, sent) is expected


def test_string_choices_still_reject_unknown_values():
    """Контроль: смягчение сравнения не должно открыть дорогу чему угодно."""
    with pytest.raises(HTTPException) as excinfo:
        _coerce_value('REFERRAL_LEVELS_MODE', 'ranks')
    assert excinfo.value.status_code == 400


def test_setting_without_choices_is_not_restricted():
    """Ограничение задаётся списком, а не самим фактом проверки."""
    assert bot_configuration_service.value_matches_choice('REFERRAL_MAX_LEVEL_DEPTH', 7) is True


class TestChoiceKeyNormalisation:
    @pytest.mark.parametrize(
        ('value', 'expected'),
        [(True, 'true'), (False, 'false'), ('TRUE', 'true'), (' tiers ', 'tiers'), (3, '3'), (None, 'none')],
    )
    def test_shapes_reduce_to_a_comparable_key(self, value, expected):
        assert bot_configuration_service.as_choice_key(value) == expected

    def test_bool_and_number_do_not_collapse_together(self):
        """``True == 1`` в Python, но ключи у них обязаны различаться.

        Иначе настройка с вариантами '1'/'0' принимала бы булево, а булева —
        единицу, и админ сохранил бы не то, что выбрал.
        """
        assert bot_configuration_service.as_choice_key(True) == 'true'
        assert bot_configuration_service.as_choice_key(1) == '1'
        assert bot_configuration_service.as_choice_key(False) != bot_configuration_service.as_choice_key(0)

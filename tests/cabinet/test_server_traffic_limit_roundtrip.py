"""Настройки премиум-сквада должны переживать сохранение тарифа из админки.

Кабинет гоняет `server_traffic_limits` через схему в обе стороны: на чтении
`admin_tariffs._get_tariff_detail` собирает `ServerTrafficLimit(**limit_data)`,
на записи `update_tariff` кладёт обратно `limit.model_dump()`. Pydantic по
умолчанию отбрасывает незадекларированные ключи, поэтому поле, которое есть в
JSON, но не объявлено в схеме, молча исчезает при первом же сохранении тарифа —
админ правит название, а вместе с ним теряет цены докупки премиум-трафика.

Тест воспроизводит этот круг целиком.
"""

from app.cabinet.schemas.tariffs import ServerTrafficLimit
from app.utils.premium_traffic import parse_premium_squad


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'

STORED = {
    'traffic_limit_gb': 5,
    'name': 'Мобильный резерв',
    'sort_order': 2,
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 20,
}


def _roundtrip(stored: dict) -> dict:
    """Прогнать запись через чтение и запись ровно так, как это делает кабинет."""
    read_back = ServerTrafficLimit(**stored)  # admin_tariffs.py, сборка ответа
    return read_back.model_dump()  # admin_tariffs.py, сохранение


def test_premium_topup_settings_survive_a_tariff_save():
    saved = _roundtrip(STORED)

    assert saved['traffic_limit_gb'] == 5
    assert saved['name'] == 'Мобильный резерв'
    assert saved['sort_order'] == 2
    assert saved['topup_enabled'] is True
    assert saved['topup_packages'] == {'1': 500, '5': 2000}
    assert saved['max_topup_gb'] == 20


def test_every_stored_key_is_declared_in_the_schema():
    """Страховка на будущее: новый ключ в JSON без поля в схеме — потеря данных."""
    saved = _roundtrip(STORED)

    assert set(STORED) <= set(saved), f'схема теряет ключи: {set(STORED) - set(saved)}'


def test_saved_form_is_still_readable_by_the_domain_parser():
    """Круг через кабинет не должен ломать разбор на стороне воркера."""
    config = parse_premium_squad(SQUAD, _roundtrip(STORED))

    assert config is not None
    assert config.limit_gb == 5
    assert config.name == 'Мобильный резерв'
    assert config.topup_enabled is True
    assert config.topup_packages == {1: 500, 5: 2000}
    assert config.max_topup_gb == 20


def test_legacy_record_without_topup_fields_gets_safe_defaults():
    """Тарифы, заведённые до появления премиум-докупки, не должны падать."""
    saved = _roundtrip({'traffic_limit_gb': 5})

    assert saved['topup_enabled'] is False
    assert saved['topup_packages'] == {}
    assert saved['max_topup_gb'] == 0
    assert parse_premium_squad(SQUAD, saved).limit_gb == 5

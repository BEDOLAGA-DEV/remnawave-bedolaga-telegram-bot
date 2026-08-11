"""Дефолты SLA в коде обязаны совпадать с .env.example.

Расхождение означало: кто поднимает бота без .env, получает включённый SLA с
порогом 5 минут и повтором раз в 15 — админам летит спам напоминаний по
каждому тикету, хотя документированный дефолт SLA выключен.
"""

import re
from pathlib import Path

import pytest

from app.config import Settings


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / '.env.example'

SLA_FIELDS = (
    'SUPPORT_TICKET_SLA_ENABLED',
    'SUPPORT_TICKET_SLA_MINUTES',
    'SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS',
    'SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES',
)


def _env_example_value(name: str) -> str:
    match = re.search(rf'^#?\s*{name}=(.*)$', ENV_EXAMPLE.read_text(encoding='utf-8'), re.MULTILINE)
    assert match, f'{name} отсутствует в .env.example'
    return match.group(1).strip()


def _coerce(raw: str, field_type: type):
    if field_type is bool:
        return raw.lower() in {'1', 'true', 'yes', 'on'}
    return field_type(raw)


@pytest.mark.parametrize('name', SLA_FIELDS)
def test_code_default_matches_env_example(name):
    field = Settings.model_fields[name]
    expected = _coerce(_env_example_value(name), field.annotation)
    assert field.default == expected, f'{name}: код по умолчанию {field.default!r}, .env.example обещает {expected!r}'


def test_sla_is_off_by_default():
    """Явно: без .env напоминания молчат."""
    assert Settings.model_fields['SUPPORT_TICKET_SLA_ENABLED'].default is False

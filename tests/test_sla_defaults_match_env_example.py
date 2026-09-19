"""Дефолты SLA в коде обязаны совпадать с .env.example.

Расхождение означало: кто поднимает бота без .env, получает включённый SLA с
порогом 5 минут и повтором раз в 15 — админам летит спам напоминаний по
каждому тикету, хотя документированный дефолт SLA выключен.

Напоминания о заявках на вывод (REFERRAL_WITHDRAWAL_REMINDER_*) устроены так же
и проверяются той же линейкой.
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

WITHDRAWAL_REMINDER_FIELDS = (
    'REFERRAL_WITHDRAWAL_REMINDER_ENABLED',
    'REFERRAL_WITHDRAWAL_REMINDER_MINUTES',
    'REFERRAL_WITHDRAWAL_REMINDER_CHECK_INTERVAL_SECONDS',
    'REFERRAL_WITHDRAWAL_REMINDER_COOLDOWN_MINUTES',
)


def _env_example_value(name: str) -> str:
    match = re.search(rf'^#?\s*{name}=(.*)$', ENV_EXAMPLE.read_text(encoding='utf-8'), re.MULTILINE)
    assert match, f'{name} отсутствует в .env.example'
    return match.group(1).strip()


def _coerce(raw: str, field_type: type):
    if field_type is bool:
        return raw.lower() in {'1', 'true', 'yes', 'on'}
    return field_type(raw)


@pytest.mark.parametrize('name', SLA_FIELDS + WITHDRAWAL_REMINDER_FIELDS)
def test_code_default_matches_env_example(name):
    field = Settings.model_fields[name]
    expected = _coerce(_env_example_value(name), field.annotation)
    assert field.default == expected, f'{name}: код по умолчанию {field.default!r}, .env.example обещает {expected!r}'


def test_sla_is_off_by_default():
    """Явно: без .env напоминания молчат."""
    assert Settings.model_fields['SUPPORT_TICKET_SLA_ENABLED'].default is False


def test_withdrawal_reminders_are_off_by_default():
    assert Settings.model_fields['REFERRAL_WITHDRAWAL_REMINDER_ENABLED'].default is False

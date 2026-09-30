"""Общее у фасада DPI//CHECKER и истории аккаунта: деньги, статус, цели строки и её подпись."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.dpichecker.targets import safe_name


STATUS_MAX = 16
USD = Decimal('0.0001')
SOURCE_SITE = 'site'  # запуск или монитор не из кабинета (сайт, их бот, API), взят в кабинет


def usd(value: Any) -> Decimal | None:
    """Сумма сервиса (число у проверок, строка у Зонда) → Decimal с 4 знаками."""
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value)).quantize(USD)
    except (InvalidOperation, ValueError):
        return None


def _status(value: Any, fallback: str) -> str:
    """Статус сервиса в колонку String(16): незнакомое длинное значение не роняет запись."""
    return str(value or fallback)[:STATUS_MAX]


def _row_targets(check_type: str | None, targets: list[dict[str, str]]) -> list[dict[str, str]]:
    kind = check_type or 'ip'
    return [{'value': str(t['value']), 'name': safe_name(kind, str(t['value']), t.get('name'))} for t in targets]


def _label(label: str, targets: list[dict[str, str]]) -> str:
    return (label or ', '.join(t['name'] for t in targets))[:255]

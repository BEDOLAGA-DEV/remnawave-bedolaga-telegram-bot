"""Чистые разбор и проверка настроек произвольного количества дней тарифа."""

from __future__ import annotations


def parse_positive_days(raw: str) -> int:
    """Целое положительное число дней."""
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError('days must be a whole number') from exc
    if value <= 0:
        raise ValueError('days must be positive')
    return value


def validate_custom_days_configuration(
    *,
    price_per_day_kopeks: int | None,
    min_days: int | None,
    max_days: int | None,
) -> tuple[str, ...]:
    """Ошибки для человека, которые не дают включить произвольные дни."""
    errors: list[str] = []

    if price_per_day_kopeks is None:
        errors.append('не указана цена за 1 день')
    elif price_per_day_kopeks <= 0:
        errors.append('цена за 1 день должна быть больше нуля')

    if min_days is None:
        errors.append('не указан минимум дней')
    elif min_days <= 0:
        errors.append('минимум дней должен быть больше нуля')

    if max_days is None:
        errors.append('не указан максимум дней')
    elif max_days <= 0:
        errors.append('максимум дней должен быть больше нуля')

    if min_days is not None and max_days is not None and min_days > 0 and max_days > 0 and max_days < min_days:
        errors.append('максимум дней не может быть меньше минимума')

    return tuple(errors)

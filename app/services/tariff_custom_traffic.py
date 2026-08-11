"""Pure parsing and validation helpers for custom-traffic tariffs."""

from decimal import Decimal, InvalidOperation


def parse_positive_rubles_to_kopeks(raw: str) -> int:
    """Parse a positive ruble amount without floating-point rounding."""
    normalized = raw.strip().replace(',', '.')
    try:
        rubles = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError('invalid price') from exc

    if not rubles.is_finite() or rubles <= 0:
        raise ValueError('price must be positive and finite')

    kopeks = rubles * Decimal(100)
    if kopeks != kopeks.to_integral_value():
        raise ValueError('price must have at most two decimal places')

    return int(kopeks)


def parse_positive_gb(raw: str) -> int:
    """Parse a positive whole-number traffic amount in gigabytes."""
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError('traffic must be a whole number') from exc

    if value <= 0:
        raise ValueError('traffic must be positive')

    return value


def validate_custom_traffic_configuration(
    *,
    price_per_gb_kopeks: int | None,
    min_traffic_gb: int | None,
    max_traffic_gb: int | None,
) -> tuple[str, ...]:
    """Return user-facing validation errors for enabling custom traffic."""
    errors: list[str] = []

    if price_per_gb_kopeks is None:
        errors.append('не указана цена за 1 ГБ')
    elif price_per_gb_kopeks <= 0:
        errors.append('цена за 1 ГБ должна быть больше нуля')

    if min_traffic_gb is None:
        errors.append('не указан минимальный объём')
    elif min_traffic_gb <= 0:
        errors.append('минимальный объём должен быть больше нуля')

    if max_traffic_gb is None:
        errors.append('не указан максимальный объём')
    elif max_traffic_gb <= 0:
        errors.append('максимальный объём должен быть больше нуля')

    if (
        min_traffic_gb is not None
        and max_traffic_gb is not None
        and min_traffic_gb > 0
        and max_traffic_gb > 0
        and max_traffic_gb < min_traffic_gb
    ):
        errors.append('максимальный объём не может быть меньше минимального')

    return tuple(errors)

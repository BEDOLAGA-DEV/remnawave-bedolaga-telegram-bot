"""Shared formatting utilities for traffic, price, and period display."""


def format_traffic(gb: int) -> str:
    """Форматирует трафик."""
    if gb == 0:
        return 'Безлимит'
    return f'{gb} ГБ'


def format_price_kopeks(kopeks: int, compact: bool = False) -> str:
    """Форматирует цену из копеек в рубли."""
    rubles = kopeks / 100
    if compact:
        # Компактный формат - округляем до рублей
        return f'{int(round(rubles))}₽'
    if rubles == int(rubles):
        return f'{int(rubles)} ₽'
    return f'{rubles:.2f} ₽'


def format_period(days: int) -> str:
    """Форматирует период."""
    mod100 = days % 100
    mod10 = days % 10
    if 11 <= mod100 <= 19:
        word = 'дней'
    elif mod10 == 1:
        word = 'день'
    elif 2 <= mod10 <= 4:
        word = 'дня'
    else:
        word = 'дней'
    return f'{days} {word}'

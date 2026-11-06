"""Shared formatting utilities for traffic, price, and period display."""

import html

from app.localization.texts import get_texts


def safe_html_name(name: str | None) -> str:
    """HTML-escape a display name for Telegram HTML messages."""
    return html.escape(name or '')


def user_html_link(user) -> str:
    """Build an HTML-safe clickable user link for Telegram messages."""
    safe = safe_html_name(user.full_name)
    if getattr(user, 'telegram_id', None):
        return f'<a href="tg://user?id={user.telegram_id}">{safe}</a>'
    return f'<b>{safe}</b>'


def format_traffic(gb: int, language: str = 'ru') -> str:
    """Format traffic, preserving Russian output for existing callers."""
    texts = get_texts(language)
    if gb == 0:
        return texts.t('FORMAT_TRAFFIC_UNLIMITED', 'Безлимит')
    return texts.t('FORMAT_TRAFFIC_GB', '{value} ГБ').format(value=gb)


def format_price_kopeks(kopeks: int, compact: bool = False) -> str:
    """Форматирует цену из копеек в рубли."""
    rubles = kopeks / 100
    if compact:
        # Компактный формат - округляем до рублей
        return f'{int(round(rubles))}₽'
    if rubles == int(rubles):
        return f'{int(rubles)} ₽'
    return f'{rubles:.2f} ₽'


def format_period(days: int, language: str = 'ru') -> str:
    """Format a day count using the requested language's plural rules."""
    code = (language or 'ru').replace('_', '-').split('-')[0].lower()
    mod100 = days % 100
    mod10 = days % 10
    if code in {'ru', 'ua', 'uk'}:
        form = 'MANY' if 11 <= mod100 <= 19 else 'ONE' if mod10 == 1 else 'FEW' if 2 <= mod10 <= 4 else 'MANY'
    else:
        form = 'ONE' if days == 1 else 'MANY'
    defaults = {'ONE': '{count} день', 'FEW': '{count} дня', 'MANY': '{count} дней'}
    return get_texts(language).t(f'FORMAT_PERIOD_DAYS_{form}', defaults[form]).format(count=days)

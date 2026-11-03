import html
import re
from datetime import UTC, datetime

from app.localization.texts import get_texts
from app.utils.subscription_time import local_days_until
from app.utils.timezone import format_local_datetime


# Формат Telegram-логина: 5-32 символа, первый — буква. Тот же шаблон используется
# в app/services/guest_purchase_service.py при приёме логина от пользователя.
_TELEGRAM_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$')


def _coerce_datetime(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        if dt == 'now' or dt == '':
            return datetime.now(UTC)
        try:
            return datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return datetime.now(UTC)
    return dt


def format_datetime(dt: datetime | str, format_str: str = '%d.%m.%Y %H:%M') -> str:
    """Момент в часовом поясе оператора (settings.TIMEZONE); без зоны — считается UTC.

    Раньше здесь был голый strftime по UTC: админка показывала окончание
    подписки 11:17, когда панель и «Моя подписка» — 14:17 по Москве.
    """
    return format_local_datetime(_coerce_datetime(dt), format_str)


def format_date(dt: datetime | str, format_str: str = '%d.%m.%Y') -> str:
    return format_local_datetime(_coerce_datetime(dt), format_str)


def _formatter_language(language: str) -> str:
    code = (language or 'ru').split('-')[0].lower()
    return 'ua' if code == 'uk' else code


def format_time_ago(dt: datetime | str, language: str = 'ru') -> str:
    dt = _coerce_datetime(dt)
    diff = datetime.now(UTC) - dt
    texts = get_texts(_formatter_language(language))
    if diff.days > 0:
        if diff.days == 1:
            return texts.t('FORMAT_YESTERDAY')
        if diff.days < 7:
            value, unit = diff.days, 'DAY'
        elif diff.days < 30:
            value, unit = diff.days // 7, 'WEEK'
        elif diff.days < 365:
            value, unit = diff.days // 30, 'MONTH'
        else:
            value, unit = diff.days // 365, 'YEAR'
    elif diff.seconds > 3600:
        value, unit = diff.seconds // 3600, 'HOUR'
    elif diff.seconds > 60:
        value, unit = diff.seconds // 60, 'MINUTE'
    else:
        return texts.t('FORMAT_JUST_NOW')
    suffix = 'ONE' if value == 1 else 'OTHER'
    return texts.t(f'FORMAT_AGO_{unit}_{suffix}').format(count=value)


def format_days_declension(days: int, language: str = 'ru') -> str:
    language_code = _formatter_language(language)
    if language_code in {'ru', 'ua'}:
        if days % 10 == 1 and days % 100 != 11:
            suffix = 'ONE'
        elif days % 10 in [2, 3, 4] and days % 100 not in [12, 13, 14]:
            suffix = 'FEW'
        else:
            suffix = 'MANY'
    else:
        suffix = 'ONE' if days == 1 else 'MANY'
    return get_texts(language_code).t(f'FORMAT_DAYS_{suffix}').format(count=days)


def format_duration(seconds: int, language: str = 'ru') -> str:
    if seconds < 60:
        value, unit = seconds, 'SECOND'
    elif seconds // 60 < 60:
        value, unit = seconds // 60, 'MINUTE'
    elif seconds // 3600 < 24:
        value, unit = seconds // 3600, 'HOUR'
    else:
        value, unit = seconds // 86400, 'DAY'
    return get_texts(_formatter_language(language)).t(f'FORMAT_DURATION_{unit}').format(count=value)


def format_bytes(bytes_value: int) -> str:
    if bytes_value == 0:
        return '0 B'

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(bytes_value)
    unit_index = 0

    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if size == int(size):
        return f'{int(size)} {units[unit_index]}'
    return f'{size:.1f} {units[unit_index]}'


def format_percentage(value: float, decimals: int = 1) -> str:
    return f'{value:.{decimals}f}%'


def format_number(number: float, separator: str = ' ') -> str:
    if isinstance(number, float):
        integer_part = int(number)
        decimal_part = number - integer_part

        formatted_integer = f'{integer_part:,}'.replace(',', separator)

        if decimal_part > 0:
            return f'{formatted_integer}.{decimal_part:.2f}'.split('.')[0] + f'.{str(decimal_part).split(".")[1][:2]}'
        return formatted_integer
    return f'{number:,}'.replace(',', separator)


def format_price_range(min_price: int, max_price: int) -> str:
    from app.config import settings

    min_formatted = settings.format_price(min_price)
    max_formatted = settings.format_price(max_price)

    if min_price == max_price:
        return min_formatted
    return f'{min_formatted} - {max_formatted}'


def truncate_text(text: str, max_length: int = 100, suffix: str = '...') -> str:
    if len(text) <= max_length:
        return text

    return text[: max_length - len(suffix)] + suffix


def format_username(username: str | None, user_id: int, full_name: str | None = None) -> str:
    if full_name:
        return full_name
    if username:
        return f'@{username}'
    return f'ID{user_id}'


def format_username_link(username: str | None, fallback: str = '') -> str:
    """Telegram-логин явной ссылкой — для rich-сообщений.

    Rich-сообщения уходят со skip_entity_detection=True (app/utils/rich_admin.py,
    app/utils/rich_menu.py), поэтому голый @username в них не подсвечивается:
    ссылку приходится ставить руками.

    Ссылка ставится только на то, что выглядит настоящим Telegram-логином.
    Колонка users.username хранит не только их: OAuth-регистрация в кабинете кладёт
    туда логин Discord/Яндекса (app/cabinet/auth/oauth_providers.py), а это чужое
    пространство имён — t.me/<логин> оттуда ведёт либо в никуда, либо на
    постороннего человека с таким же ником. Остальное отдаём текстом, как было.
    """
    if not username:
        return fallback

    normalized_username = username.lstrip('@')
    if not normalized_username:
        return fallback

    safe_username = html.escape(normalized_username, quote=True)
    if not _TELEGRAM_USERNAME_RE.match(normalized_username):
        return f'@{safe_username}'
    return f'<a href="https://t.me/{safe_username}">@{safe_username}</a>'


def format_subscription_status(is_active: bool, is_trial: bool, end_date: datetime | str, language: str = 'ru') -> str:
    if isinstance(end_date, str):
        try:
            end_date = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            end_date = datetime.now(UTC)

    texts = get_texts(_formatter_language(language))
    if not is_active:
        return texts.t('FORMAT_STATUS_INACTIVE')
    status = texts.t('FORMAT_STATUS_TRIAL' if is_trial else 'FORMAT_STATUS_ACTIVE')
    now = datetime.now(UTC)
    if end_date > now:
        days_left = local_days_until(end_date, now)
        if days_left > 0:
            status += texts.t('FORMAT_STATUS_DAYS').format(count=days_left)
        else:
            hours_left = (end_date - now).seconds // 3600
            status += texts.t('FORMAT_STATUS_HOURS').format(count=hours_left)
    else:
        status = texts.t('FORMAT_STATUS_EXPIRED')
    return status


def format_traffic_usage(used_gb: float, limit_gb: int, language: str = 'ru') -> str:
    unit = get_texts(_formatter_language(language)).t('FORMAT_GB')
    if limit_gb == 0:
        return f'{used_gb:.1f} {unit} / ∞'
    percentage = (used_gb / limit_gb) * 100 if limit_gb > 0 else 0
    return f'{used_gb:.1f} {unit} / {limit_gb} {unit} ({percentage:.1f}%)'


def format_boolean(value: bool, language: str = 'ru') -> str:
    return get_texts(_formatter_language(language)).t('FORMAT_YES' if value else 'FORMAT_NO')

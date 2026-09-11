"""Ошибки /v1/geo словами для админа (контракт 2026-09-11). Неизвестный код — сообщение сервиса с кодом."""

from __future__ import annotations

from app.external.bschek_api import BschekAPIError


TOO_MANY_TARGETS_MESSAGE = 'GEO проверяет не больше 20 целей за запуск'

_STATIC_MESSAGES = {
    'insufficient_credits': 'На балансе bschekbot не хватает кредитов на резерв этой проверки',
    'tier_too_low': 'Эта проверка недоступна на текущем тарифе bschekbot: GEO открыт с тарифа Bronze',
    'paid_only': 'Эта проверка доступна только на платном тарифе bschekbot',
    'api_not_available': 'Проверка недоступна для этого ключа API bschekbot',
    'port_not_allowed': 'Порт цели запрещён у сервиса, проверка не запущена (денег не стоит)',
    'heavy_requires_domain': '«Тяжёлой» пробе нужна хотя бы одна доменная цель',
    'no_nodes': 'Под этот охват у сервиса нет ни одного города',
    'too_many_active': 'У сервиса уже идут три прогона этого аккаунта — ждём и повторяем',
    'maintenance': 'Сервис на обслуживании — повторяем позже',
    'catalog_unavailable': 'Справочник городов сервиса недоступен, повторите позже',
    'not_running': 'Прогон уже завершён — останавливать нечего',
    'cannot_cancel': 'Прогон оборван на стороне сервиса, он закроет его сам',
}
_UNKNOWN_WHAT = {
    'unknown_city': 'Город',
    'unknown_region': 'Регион',
    'unknown_district': 'Округ',
    'unknown_isp': 'Провайдер',
}


def _unknown_message(code: str, details: dict) -> str:
    unknown = details.get('unknown') or []
    listed = f': {", ".join(str(item) for item in unknown)}' if unknown else ''
    return f'{_UNKNOWN_WHAT[code]} не найден в справочнике сервиса{listed}'


def geo_error_message(exc: BschekAPIError) -> str:
    """Текст для человека по коду сервиса; подробности (`details`) вплетаются в текст, где они есть."""
    details = exc.details or {}
    code = exc.code
    if code == 'too_many_nodes':
        limit = details.get('suggested_city_limit')
        tail = f': сервис предлагает потолок {limit} городов' if limit else ''
        return f'Слишком много городов для этого режима{tail}'
    if code == 'too_many_targets':
        # У probe тот же код с другим лимитом — сообщение сервиса точнее нашего.
        return exc.message or TOO_MANY_TARGETS_MESSAGE
    if code in _UNKNOWN_WHAT:
        return _unknown_message(code, details)
    if code == 'rate_limited':
        wait = f' через {int(exc.retry_after)} с' if exc.retry_after else ''
        return f'Слишком часто: сервис просит повторить{wait}'
    if code in ('subscription_error', 'resolve_failed'):
        return f'Сервис не смог развернуть подписку: {exc.message}'
    static = _STATIC_MESSAGES.get(code)
    if static:
        return static
    return f'{exc.message} [{code}]'

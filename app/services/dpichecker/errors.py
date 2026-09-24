"""Исключения домена DPI//CHECKER и тексты для людей по коду отказа сервиса."""

from __future__ import annotations

from app.external.dpichecker_api import DpiCheckerAPIError, DpiCheckerGatewayError


class DpiCheckerDisabled(Exception):
    """Модуль выключен или не настроен — сказать словами, что сделать."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ActionNotFound(Exception):
    """Нет такой строки действия (или она другого вида)."""


class LaunchRefused(Exception):
    """Сервис отказал по нашему запросу: статус и слова для человека."""

    def __init__(self, *, code: str, message: str, status: int, rejected: list[str]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.rejected = rejected


HUMAN_ERRORS: dict[str, str] = {
    'invalid_api_key': 'Ключ API DPI//CHECKER неверный — проверьте его в настройках',
    'missing_api_key': 'Не задан ключ API DPI//CHECKER',
    'ip_not_allowed': 'Ключ API ограничен по IP — добавьте адрес сервера бота в белый список ключа на сайте DPI//CHECKER',
    'api_not_unlocked': 'API DPI//CHECKER ещё не открыт: нужен депозит от $1',
    'account_banned': 'Аккаунт DPI//CHECKER заблокирован',
    'insufficient_balance': 'Не хватает денег на балансе DPI//CHECKER — Пополните баланс',
    'invalid_request': 'DPI//CHECKER не принял запрос — проверьте, что введено',
    'invalid_location': 'Такой страны нет в DPI//CHECKER',
    'invalid_pops': 'Часть выбранных точек сейчас недоступна — обновите список и выберите заново',
    'too_many_resources': 'Слишком много адресов за раз: не больше 50',
    'private_target': 'Локальные и частные адреса проверить нельзя — они не видны из интернета',
    'blacklisted': 'DPI//CHECKER не проверяет эти адреса (они в его чёрном списке)',
    'not_found': 'DPI//CHECKER не нашёл эту проверку',
    'not_cancellable': 'Проверка уже идёт или закончилась — отменить нельзя',
    'idempotency_conflict': 'Этот запуск уже был с другими данными — запустите заново',
    'idempotency_in_flight': 'Этот запуск ещё обрабатывается — подождите минуту',
    'rate_limited': 'Слишком часто — подождите немного и повторите',
    'quota_exceeded': 'Лимит на сегодня исчерпан',
    'maintenance': 'DPI//CHECKER на обслуживании — попробуйте позже',
    'bot_unavailable': 'DPI//CHECKER сейчас недоступен — попробуйте позже',
    'internal': 'У DPI//CHECKER внутренняя ошибка — попробуйте позже',
}
GATEWAY_TEXT = 'DPI//CHECKER не ответил — попробуйте позже'


def human_error(exc: DpiCheckerAPIError) -> str:
    """Слова для человека — по стабильному коду; неизвестный код — текст сервиса."""
    if isinstance(exc, DpiCheckerGatewayError):
        return GATEWAY_TEXT
    text = HUMAN_ERRORS.get(exc.code)
    if text is None:
        return f'DPI//CHECKER отказал: {exc.message or exc.code}'
    if exc.rejected:
        return f'{text}: {", ".join(exc.rejected[:10])}'
    return text

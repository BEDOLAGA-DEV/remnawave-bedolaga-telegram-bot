"""Factory for creating Bot instances with proxy support."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import TelegramAPIServer
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import DEFAULT_TELEGRAM_BOT_API_BASE_URL, settings

_API_PLACEHOLDERS = frozenset({'{token}', '{method}'})
_FILE_PLACEHOLDERS = frozenset({'{token}', '{path}'})


def _is_full_api_pattern(url: str) -> bool:
    """Проверяет, что URL содержит полный паттерн API: {token} и {method}."""
    return '{token}' in url and '{method}' in url


def _is_full_file_pattern(url: str) -> bool:
    """Проверяет, что URL содержит полный паттерн file: {token} и {path}."""
    return '{token}' in url and '{path}' in url


def _has_any_placeholder(url: str, placeholders: frozenset[str]) -> bool:
    return any(p in url for p in placeholders)


def _validate_or_expand(
    base: str,
    *,
    is_api: bool,
) -> str:
    """Возвращает паттерн: либо валидный кастомный, либо сгенерированный через from_base."""
    placeholders = _API_PLACEHOLDERS if is_api else _FILE_PLACEHOLDERS
    is_full = _is_full_api_pattern(base) if is_api else _is_full_file_pattern(base)
    
    if is_full:
        try:
            base.format(token='x', method='y') if is_api else base.format(token='x', path='y')
        except KeyError as e:
            raise ValueError(
                f'Telegram API URL: некорректный паттерн, ожидаются {sorted(placeholders)!r}. Ошибка: {e}'
            ) from e
        return base
    
    if _has_any_placeholder(base, placeholders):
        required = ', '.join(sorted(placeholders))
        raise ValueError(
            f'Telegram API URL содержит частичный паттерн (есть placeholder, но не все). '
            f'Требуются оба: {required}. Получено: {base!r}'
        )
    
    server = TelegramAPIServer.from_base(base)
    return server.base if is_api else server.file


def _needs_custom_session(proxy_url: str | None, api_base: str, file_base: str) -> bool:
    """Кастомная сессия нужна при прокси или при URL, отличном от дефолтного."""
    if proxy_url is not None:
        return True
    if api_base != DEFAULT_TELEGRAM_BOT_API_BASE_URL or file_base != DEFAULT_TELEGRAM_BOT_API_BASE_URL:
        return True
    return False


def create_bot(
    token: str | None = None,
    session: AiohttpSession | None = None,
    **kwargs
) -> Bot:
    """Create a Bot instance with SOCKS5 proxy session if PROXY_URL is configured.
    You should provide custom session.
    """
    proxy_url = settings.get_proxy_url()
    api_base = settings.TELEGRAM_BOT_API_BASE_URL
    file_base = settings.TELEGRAM_BOT_FILE_BASE_URL

    if session is None and _needs_custom_session(proxy_url, api_base, file_base):
        if api_base == file_base and not _has_any_placeholder(api_base, _API_PLACEHOLDERS | _FILE_PLACEHOLDERS):
            api = TelegramAPIServer.from_base(api_base)
        else:
            base_pattern = _validate_or_expand(api_base, is_api=True)
            file_pattern = _validate_or_expand(file_base, is_api=False)
            api = TelegramAPIServer(base=base_pattern, file=file_pattern)
        session = AiohttpSession(api=api, proxy=proxy_url)

    kwargs.setdefault('default', DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)

"""Фабрика экземпляров Telegram Bot с поддержкой прокси."""

from urllib.parse import urlparse

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import settings

logger = structlog.get_logger(__name__)


def get_bot(token: str | None = None, session: AiohttpSession | None = None) -> Bot:
    """
    Создаёт и возвращает настроенный экземпляр Bot.

    Если PROXY_URL задан в настройках и сессия не передана,
    автоматически создаёт AiohttpSession с прокси.

    Поддерживает использование как контекстного менеджера:
        async with get_bot() as bot:
            await bot.send_message(...)

    Args:
        token: Токен бота. По умолчанию берётся из settings.BOT_TOKEN.
        session: Готовая AiohttpSession. Если не передана — создаётся автоматически.

    Returns:
        Настроенный экземпляр Bot с прокси (если задан) и ParseMode.HTML.
    """
    token = token or settings.BOT_TOKEN

    if session is None:
        proxy_url = settings.get_proxy_url()
        if proxy_url:
            try:
                parsed = urlparse(proxy_url)
                proxy_host = f'{parsed.hostname}:{parsed.port}' if parsed.port else parsed.hostname
                logger.info('Создание бота через прокси', proxy=proxy_host)
            except Exception:
                logger.info('Создание бота через прокси')

            session = AiohttpSession(proxy=proxy_url)

    return Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

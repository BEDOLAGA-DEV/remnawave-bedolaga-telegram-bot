"""Factory for creating Bot instances with proxy and custom API server support."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import settings


def create_bot(token: str | None = None, **kwargs) -> Bot:
    """Create a Bot instance with SOCKS5 proxy and/or custom Telegram API server."""
    proxy_url = settings.get_proxy_url()
    telegram_api_url = settings.get_telegram_api_url()
    session = None
    if proxy_url or telegram_api_url:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        session_kwargs: dict = {}
        if proxy_url:
            session_kwargs["proxy"] = proxy_url
        if telegram_api_url:
            session_kwargs["api"] = TelegramAPIServer.from_base(telegram_api_url)

        session = AiohttpSession(**session_kwargs)

    kwargs.setdefault("default", DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Bot(token=token or settings.BOT_TOKEN, session=session, **kwargs)
